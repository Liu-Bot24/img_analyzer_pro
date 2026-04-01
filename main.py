import os
from PIL import Image
import io
import base64
import requests
import csv
import shutil
import re
import yaml
import time
import logging
import threading
import queue
from concurrent.futures import ThreadPoolExecutor

# ==========================================
# 核心功能：热加载配置文件
# ==========================================
def load_config():
    """实时从磁盘读取最新的 config.yaml"""
    try:
        with open("config.yaml", 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except Exception as e:
        # 如果用户正在保存文件导致冲突，稍等一下返回 None
        return None

# 初始加载，用于确定路径和并发数
initial_config = load_config()
if not initial_config:
    print("错误：无法读取配置文件 config.yaml")
    exit(1)

# 路径归一化
SOURCE_DIR = os.path.normpath(initial_config['image']['source_dir'])

# 日志初始化（保持初始路径，避免运行中日志文件跳变）
log_path = os.path.join(SOURCE_DIR, initial_config['system']['log_file'])
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler(log_path, encoding='utf-8'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# 全局写入锁
write_lock = threading.Lock()

# 线程分配队列
worker_queue = queue.Queue()

def safe_csv_operation(file_path, mode, callback, *args, **kwargs):
    while True:
        try:
            with open(file_path, mode, newline="", encoding="utf-8-sig") as f:
                return callback(f, *args, **kwargs)
        except PermissionError:
            logger.warning(f"⚠️ 文件被占用: {file_path}。请关闭 Excel。5秒后自动重试...")
            time.sleep(5)
        except Exception as e:
            logger.error(f"CSV 操作发生未知错误: {e}")
            raise

def extract_image_frame(image_path, image_settings):
    try:
        with Image.open(image_path) as img:
            if img.mode != 'RGB':
                img = img.convert('RGB')
            
            width, height = img.size
            max_dim = image_settings.get('max_dimension', 1024)
            if max(height, width) > max_dim:
                scale = max_dim / max(height, width)
                new_size = (int(width * scale), int(height * scale))
                resample_filter = getattr(Image, 'Resampling', Image).LANCZOS
                img = img.resize(new_size, resample_filter)
            
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=85)
            b64_str = base64.b64encode(buffer.getvalue()).decode('utf-8')
            return f"data:image/jpeg;base64,{b64_str}"
    except Exception as e:
        logger.error(f"处理图片失败 {image_path}: {e}")
        return None
        
        height, width = image.shape[:2]
        max_dim = image_settings.get('max_dimension', 1024)
        if max(height, width) > max_dim:
            scale = max_dim / max(height, width)
            image = cv2.resize(image, (int(width * scale), int(height * scale)))
            
        _, buffer = cv2.imencode('.jpg', image, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        b64_str = base64.b64encode(buffer).decode('utf-8')
        return f"data:image/jpeg;base64,{b64_str}"
    except Exception as e:
        logger.error(f"处理图片失败 {image_path}: {e}")
        return None

def analyze_image(image_path, api_settings, image_settings, category_map, system_settings):
    frame = extract_image_frame(image_path, image_settings)
    if not frame: return None

    categories_list = "\n".join([f"- {name}: {info['desc']}" for name, info in category_map.items()])
    prompt = system_settings['prompt_template'].format(categories_list=categories_list)

    payload = {
        "model": api_settings['model'],
        "messages": [
            {
                "role": "user", 
                "content": [
                    {"type": "text", "text": prompt}, 
                    {"type": "image_url", "image_url": {"url": frame}}
                ]
            }
        ],
        "temperature": api_settings['temperature'],
        "max_tokens": api_settings['max_tokens']
    }
    headers = {"Authorization": f"Bearer {api_settings['key']}", "Content-Type": "application/json"}

    for attempt in range(api_settings['max_retries'] + 1):
        try:
            response = requests.post(f"{api_settings['base_url']}/chat/completions", json=payload, headers=headers, timeout=api_settings['timeout'])
            if response.ok:
                return response.json()['choices'][0]['message']['content']
            logger.warning(f"API 错误 (尝试 {attempt+1}/{api_settings['max_retries']+1}): {response.text}")     
        except Exception as e:
            logger.warning(f"请求异常 (尝试 {attempt+1}/{api_settings['max_retries']+1}): {e}")
        if attempt < api_settings['max_retries']: time.sleep(2)
    return None

def process_one_image(image_path):
    worker_idx = worker_queue.get()
    try:
        # --- 热加载逻辑开始 ---
        current_config = load_config()
        while not current_config:
            time.sleep(1) # 如果读不到（正在编辑），等一秒再读
            current_config = load_config()

        api_cfg = current_config['api'].copy()
        image_cfg = current_config['image']
        cat_map = current_config['categories']
        sys_cfg = current_config['system']

        endpoints = api_cfg.get('endpoints', [])
        if endpoints:
            # 动态获取当前线程对应的模型配置
            idx = worker_idx if worker_idx < len(endpoints) else (worker_idx % len(endpoints))
            endpoint = endpoints[idx]
            api_cfg['key'] = endpoint['key']
            api_cfg['base_url'] = endpoint['base_url']
            api_cfg['model'] = endpoint['model']
        # --- 热加载逻辑结束 ---

        filename = os.path.basename(image_path)
        result = analyze_image(image_path, api_cfg, image_cfg, cat_map, sys_cfg)

        if not result:
            logger.warning(f"❌ {filename} 识别失败，跳过。")
            return

        # 使用配置中定义的第一个分类作为默认值，防止硬编码导致崩溃
        category = list(cat_map.keys())[0] if cat_map else "photo"

        match_cat = re.search(r'\[Category:\s*(.*?)\]', result, re.IGNORECASE)
        if match_cat:
            cat_key = match_cat.group(1).lower().strip()
            if cat_key in cat_map:
                category = cat_key
            else:
                logger.warning(f"⚠️ AI 返回了未定义的分类: {cat_key}，已回退到默认分类: {category}")

        title = "未命名图片"
        match_title = re.search(r'\[Title:\s*(.*?)\]', result, re.IGNORECASE)
        if match_title:
            title = match_title.group(1).strip()[:40]
        else:
            title = re.sub(r'\[.*?\]', '', result).strip()[:40].replace('\n', ' ')

        csv_path = os.path.join(SOURCE_DIR, sys_cfg['csv_file'])

        with write_lock:
            def write_callback(f):
                writer = csv.writer(f)
                writer.writerow([filename, result, title])

            try:
                safe_csv_operation(csv_path, "a", write_callback)
                dest_folder = os.path.join(SOURCE_DIR, os.path.normpath(cat_map[category]['path']))
                os.makedirs(dest_folder, exist_ok=True)

                safe_title = re.sub(r'[\\/:*?"<>|]', '', title).strip()
                name_part, ext_part = os.path.splitext(filename)

                if image_cfg['auto_rename']:
                    if image_cfg.get('keep_original_name', True):
                        base_name = f"{safe_title}_{name_part}"
                    else:
                        base_name = safe_title
                else:
                    base_name = name_part

                final_name = f"{base_name}{ext_part}"
                final_path = os.path.join(dest_folder, final_name)
                counter = 1
                while os.path.exists(final_path):
                    final_name = f"{base_name}_{counter}{ext_part}"
                    final_path = os.path.join(dest_folder, final_name)
                    counter += 1

                shutil.move(image_path, final_path)
                logger.info(f"✅ {filename} -> {category} | 模型: {api_cfg.get('model', 'unknown')} | 存为: {final_name}")
            except Exception as e:
                logger.error(f"落地执行失败 {filename}: {e}")
    finally:
        worker_queue.put(worker_idx)

def main():
    initial_sys_cfg = initial_config['system']

    # 确定并发数并初始化队列
    endpoints = initial_config['api'].get('endpoints', [])
    if endpoints:
        concurrency = len(endpoints)
    else:
        concurrency = initial_sys_cfg.get('concurrency', 1)

    for i in range(concurrency):
        worker_queue.put(i)

    csv_path = os.path.join(SOURCE_DIR, initial_sys_cfg['csv_file'])

    if not os.path.exists(csv_path):
        def init_callback(f):
            csv.writer(f).writerow(["Filename", "Full Result", "Title"])
        safe_csv_operation(csv_path, "w", init_callback)
    else:
        # 检查现存的 CSV 是否有 UTF-8 BOM，没有则补上，防止 Excel 打开乱码
        try:
            with open(csv_path, 'r+b') as f:
                if f.read(3) != b'\xef\xbb\xbf':
                    f.seek(0)
                    content = f.read()
                    f.seek(0)
                    f.write(b'\xef\xbb\xbf' + content)
        except Exception as e:
            logger.warning(f"检查/修复 CSV BOM 失败: {e}")

    def read_callback(f):
        res = set()
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if row: res.add(row[0])
        return res
    processed_files = safe_csv_operation(csv_path, "r", read_callback)

    all_images = [os.path.join(SOURCE_DIR, f) for f in os.listdir(SOURCE_DIR)
                  if os.path.isfile(os.path.join(SOURCE_DIR, f))
                  and os.path.splitext(f)[1].lower() in initial_config['image']['extensions']]

    pending_images = [v for v in all_images if os.path.basename(v) not in processed_files]
    logger.info(f"扫描完成: 总计 {len(all_images)}，已处理 {len(processed_files)}，待处理 {len(pending_images)} 。支持热切换配置。")

    if pending_images:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            executor.map(process_one_image, pending_images)
    else:
        logger.info("任务已全部完成。")

if __name__ == "__main__":
    main()
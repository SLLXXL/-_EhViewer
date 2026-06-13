import re
import asyncio
import urllib.parse
import os
import shutil
import time
import tempfile
from typing import List, Optional, Dict, Tuple
import aiohttp
from bs4 import BeautifulSoup
from PIL import Image as PILImage

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Node, Plain, Image

# ===================== 全局配置 =====================
TARGET_SITE = {
    "name": "E站搜索",
    "search_url": "https://e-hentai.org/?f_search={keyword}",
    "root_domain": "https://e-hentai.org"
}

# 网络请求参数
REQUEST_TIMEOUT = 60
IMG_DOWNLOAD_TIMEOUT = 50
MAX_RETRY = 10        # 图片下载最大重试次数
RETRY_DELAY = 0.5     # 下载重试间隔

# 图片发送基础配置
SINGLE_IMG_SEND_DELAY = 0.1    # 图片固定发送间隔

# 图片数量限制
MAX_IMG_COUNT = 500

# 缓存根目录
CACHE_ROOT = os.path.join(os.path.dirname(__file__), "img_cache")
INVALID_IMG_SIZE = 2 * 1024

# 站点请求头
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/134.0.0.0 Safari/537.36'
BASE_COOKIES = {"ipb_member_id": "", "ipb_pass_hash": ""}

# 二次确认关键词正则
CONFIRM_REG = r"^(是|否|对|不|yes|no)$"
# 单张图片下载耗时（用于计算预估总时间）
PER_IMG_PROCESS_DELAY = 1.4

# 搜索结果配置
MAX_SHOW_COUNT = 50  # 最多展示图集上限

# 图片发送重试规则
SEND_TRY_PER_IMG = 2    # 单张图单次轮次最多尝试 2 次
MAX_SEND_ROUND = 5      # 失败图片整体轮回最多 5 轮

# ===================== 链接转换 + 校验 =====================
def convert_torrents_url(origin_url: str) -> Optional[str]:
    """
    将 gallerytorrents.php 种子链接 转为 标准图集链接
    规则：
    错误链接：https://e-hentai.org/gallerytorrents.php?gid=3904513&t=c9865599bd
    标准链接：https://e-hentai.org/g/3904513/c9865599bd/
    """
    parsed = urllib.parse.urlparse(origin_url)
    query_dict = urllib.parse.parse_qs(parsed.query)
    gid_list = query_dict.get("gid")
    t_list = query_dict.get("t")

    if not gid_list or not t_list:
        return None
    gid = gid_list[0]
    token = t_list[0]
    # 拼接标准图集URL
    standard_url = f"{TARGET_SITE['root_domain']}/g/{gid}/{token}/"
    return standard_url

def is_valid_gallery_url(url: str) -> bool:
    """校验是否为合法E站图集链接"""
    root = TARGET_SITE["root_domain"]
    # 合法格式：https://e-hentai.org/g/xxx/xxx/
    if url.startswith(f"{root}/g/"):
        return True
    # 种子链接后续转换
    if "gallerytorrents.php" in url:
        return True
    return False

# ===================== 工具函数 =====================
def _init_cache_root():
    """初始化缓存根目录"""
    if not os.path.exists(CACHE_ROOT):
        try:
            os.makedirs(CACHE_ROOT, mode=0o777)
            logger.info(f"[根目录初始化] {CACHE_ROOT}")
        except Exception as e:
            logger.error(f"[根目录创建失败]: {str(e)}")

def create_unique_subdir(uid: str) -> str:
    """
    创建【用户ID+时间戳】唯一子文件夹，目录不重复
    :param uid: 操作用户ID
    :return: 唯一目录绝对路径
    """
    timestamp = int(time.time())
    dir_name = f"task_{uid}_{timestamp}"
    sub_dir = os.path.join(CACHE_ROOT, dir_name)
    try:
        os.makedirs(sub_dir, mode=0o777)
        logger.info(f"[创建任务目录] {sub_dir}")
        return sub_dir
    except Exception as e:
        logger.error(f"[创建任务目录失败]: {str(e)}")
        return CACHE_ROOT

def remove_dir(target_dir: str):
    """删除整个任务目录仅子目录"""
    if not os.path.exists(target_dir) or target_dir == CACHE_ROOT:
        return
    try:
        shutil.rmtree(target_dir)
        logger.info(f"[清理任务目录] {target_dir}")
    except Exception as e:
        logger.error(f"[删除目录失败]: {str(e)}")

def clear_img_cache():
    """全局清理根目录下所有旧任务文件夹"""
    if not os.path.exists(CACHE_ROOT):
        return
    for item in os.listdir(CACHE_ROOT):
        item_path = os.path.join(CACHE_ROOT, item)
        if os.path.isdir(item_path) and item.startswith("task_"):
            try:
                shutil.rmtree(item_path)
                logger.info(f"[全局清理] 删除旧任务目录: {item_path}")
            except Exception as e:
                logger.error(f"[全局清理失败] {item_path}: {str(e)}")

def get_img_path(base_dir: str, index: int) -> str:
    """根据当前任务目录 + 序号，生成图片路径（1.png/2.png）"""
    return os.path.join(base_dir, f"{index}.png")

async def download_and_convert_to_png(img_url: str, img_index: int, base_dir: str) -> Optional[str]:
    """WebP下载 → 转PNG，使用当前任务独立目录，重试逻辑"""
    png_path = get_img_path(base_dir, img_index)
    if os.path.exists(png_path):
        logger.info(f"[缓存命中] 第{img_index}张图: {png_path}")
        return png_path

    logger.info(f"[开始下载] 第{img_index}张图片: {img_url}")
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": TARGET_SITE["root_domain"],
        "Accept": "image/webp,image/*,*/*;q=0.8"
    }
    timeout = aiohttp.ClientTimeout(total=IMG_DOWNLOAD_TIMEOUT)
    download_success = False
    webp_temp_path = None

    for retry_cnt in range(1, MAX_RETRY + 1):
        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False), timeout=timeout) as session:
                async with session.get(img_url, headers=headers, cookies=BASE_COOKIES) as resp:
                    if resp.status != 200:
                        logger.warning(f"[下载重试{retry_cnt}] 状态码异常")
                        await asyncio.sleep(RETRY_DELAY)
                        continue
                    content = await resp.read()
                    content_size = len(content)
                    if content_size < INVALID_IMG_SIZE:
                        logger.error(f"[无效文件] 第{img_index}张图被防盗链拦截")
                        return None
                    with tempfile.NamedTemporaryFile(suffix=".webp", delete=False) as tmp_file:
                        tmp_file.write(content)
                        webp_temp_path = tmp_file.name
                    download_success = True
                    break
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning(f"[下载异常 重试{retry_cnt}]: {str(e)}")
            await asyncio.sleep(RETRY_DELAY)

    if not download_success or not webp_temp_path:
        logger.error(f"[最终下载失败] 第{img_index}张图（已重试{MAX_RETRY}次）")
        return None

    try:
        with PILImage.open(webp_temp_path) as pil_img:
            pil_img.save(png_path, format="PNG")
        logger.info(f"[转码成功] {webp_temp_path} → {png_path}")
    except Exception as e:
        logger.error(f"[转码失败] 第{img_index}张图: {str(e)}")
        return None
    finally:
        if os.path.exists(webp_temp_path):
            os.unlink(webp_temp_path)
    return png_path

# ===================== 插件主体 =====================
@register("eh_search_plugin", "E站搜索", "6.2.1", "torrents链接自动转换+补全导入")
class GalSearchPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.current_keyword = ""
        self.is_querying = False
        self.querying_keyword = ""
        self.user_search_cache: Dict[str, List[dict]] = {}
        # 存储结构：{用户ID: (图片路径列表, 任务目录)}
        self.wait_confirm: Dict[str, Tuple[List[str], str]] = {}
        _init_cache_root()

    async def fetch_site_data(self):
        """拉取搜索结果列表，兼容境外网络异常 + 精准抓取图集链接"""
        url_template = TARGET_SITE["search_url"]
        root = TARGET_SITE["root_domain"]
        res = []
        kw = urllib.parse.quote(self.current_keyword.strip())
        target = url_template.format(keyword=kw)
        logger.info(f"[搜索请求] 关键词: {self.current_keyword}")

        headers = {
            "User-Agent": USER_AGENT,
            "Referer": root,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9"
        }
        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False),
                                             timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as session:
                async with session.get(target, headers=headers, cookies=BASE_COOKIES) as resp:
                    if resp.status != 200:
                        return ["❌ 网页访问失败（境外站点无法连接）"]
                    html = await resp.text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.error(f"[搜索页面异常]: {str(e)}")
            return ["❌ 检测到境外网页无法访问，请检查网络后重试"]

        soup = BeautifulSoup(html, "html.parser")
        all_tr = soup.find_all("tr")
        for tr in all_tr:
            name_td = tr.find("td", class_="gl3c glname")
            if not name_td:
                continue

            a_tag = name_td.find("a", href=True)
            if not a_tag:
                continue

            gallery_href = a_tag.get("href", "").strip()

            detail_url = urllib.parse.urljoin(root, gallery_href)

            # ===================== 转换torrents链接 =====================
            if "gallerytorrents.php" in detail_url:
                logger.warning(f"[捕获种子链接，自动转换] {detail_url}")
                converted_url = convert_torrents_url(detail_url)
                if converted_url:
                    detail_url = converted_url
                else:
                    logger.error(f"[链接转换失败，跳过本条] {detail_url}")
                    continue

            # ===================== 过滤无效链接 =====================
            if not is_valid_gallery_url(detail_url):
                logger.debug(f"[无效链接，跳过] {detail_url}")
                continue

            title_div = tr.find("div", class_="glink")
            if not title_div:
                continue
            title = title_div.get_text(strip=True)

            res.append({"title": title, "detail_url": detail_url})
        return res

    # 动态无限分页最多抓取500张图
    async def get_s_links_from_gallery_page(self, base_gallery_url: str) -> List[str]:
        """自动遍历所有分页，抓取图片链接，上限 500 张"""
        s_links = []
        page_num = 0
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": base_gallery_url,
            "Accept": "text/html,application/xhtml+xml"
        }

        while True:
            page_url = f"{base_gallery_url}?p={page_num}"
            current_page_has_img = False

            try:
                async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False),
                                                 timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as session:
                    async with session.get(page_url, headers=headers, cookies=BASE_COOKIES) as resp:
                        if resp.status != 200:
                            logger.warning(f"分页 p={page_num} 访问失败，终止翻页")
                            break
                        html = await resp.text(encoding="utf-8", errors="replace")
            except Exception as e:
                logger.error(f"分页 p={page_num} 网络异常: {str(e)}，终止翻页")
                break

            soup = BeautifulSoup(html, "html.parser")
            a_list = soup.find_all("a", href=True)
            for a_tag in a_list:
                raw_href = a_tag.get("href", "").strip()
                if "/s/" in raw_href:
                    full_link = urllib.parse.urljoin(TARGET_SITE["root_domain"], raw_href)
                    if full_link not in s_links:
                        s_links.append(full_link)
                        current_page_has_img = True

                    if len(s_links) >= MAX_IMG_COUNT:
                        logger.info(f"已达到最大图片上限 {MAX_IMG_COUNT} 张，停止抓取")
                        return s_links

            # 当前分页无图片 = 遍历完所有分页
            if not current_page_has_img:
                logger.info(f"所有分页遍历完成，共抓取 {len(s_links)} 张图片")
                break

            page_num += 1
            await asyncio.sleep(0.3)  # 分页请求间隔，防反爬

        return s_links[:MAX_IMG_COUNT]

    async def get_original_img_from_s_page(self, s_url: str) -> Optional[str]:
        """解析原图直链，增加重试，改善外网访问失败问题"""
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": s_url,
            "Accept": "text/html,image/webp"
        }
        for retry_cnt in range(1, MAX_RETRY + 1):
            try:
                async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False),
                                                 timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as session:
                    async with session.get(s_url, headers=headers, cookies=BASE_COOKIES) as resp:
                        if resp.status != 200:
                            logger.warning(f"[解析链接 重试{retry_cnt}] 状态码异常")
                            await asyncio.sleep(RETRY_DELAY)
                            continue
                        html = await resp.text(encoding="utf-8", errors="replace")
            except Exception as e:
                logger.warning(f"[解析链接异常 重试{retry_cnt}]: {str(e)}")
                await asyncio.sleep(RETRY_DELAY)
                continue

            soup = BeautifulSoup(html, "html.parser")
            img_tag = soup.find("img", id="img")
            if img_tag:
                return img_tag.get("src", "").strip()

        logger.error(f"[解析原图链接最终失败] 链接: {s_url}（已重试{MAX_RETRY}次）")
        return None

    # 指令：查e站
    @filter.command("查e站")
    async def search_e_site(self, event: AstrMessageEvent):
        clear_img_cache()
        args = event.message_str.split(maxsplit=1)
        if len(args) < 2:
            yield event.plain_result("⚠️ 用法：/查e站 搜索内容")
            return
        if self.is_querying:
            yield event.plain_result(f"⚠️ 正在查询【{self.querying_keyword}】，请稍后再试")
            return

        self.current_keyword = args[1].strip()
        self.is_querying = True
        self.querying_keyword = self.current_keyword
        user_qq = event.get_sender_id()
        bot_qq = event.get_self_id()

        try:
            result_list = await self.fetch_site_data()
            if isinstance(result_list[0], str):
                yield event.plain_result(result_list)
                return

            show_list = result_list[:MAX_SHOW_COUNT]

            if not show_list:
                yield event.plain_result("⚠️ 未找到含图片的图集内容")
                return

            self.user_search_cache[user_qq] = show_list
            msg = [f"🔍 关键词【{self.current_keyword}】搜索结果（共{len(show_list)}条）\n"]
            for idx, item in enumerate(show_list, 1):
                msg.append(f"{idx}. {item['title']}")
            msg.append("\n请回复【数字】选择条目")
            node = Node(
                uin=bot_qq,
                name="🔍 EhViewer搜索结果",
                content=[Plain("\n".join(msg))]
            )
            yield event.chain_result([node])
        finally:
            self.is_querying = False
            self.querying_keyword = ""

    # 回复数字选择图集
    @filter.regex(r"^\d+$")
    async def select_gallery(self, event: AstrMessageEvent):
        select_num_str = event.message_str.strip()
        if not select_num_str.isdigit():
            return
        select_num = int(select_num_str)
        user_id = event.get_sender_id()

        if self.is_querying:
            yield event.plain_result("⚠️ 正在加载图片，请稍后操作！")
            return
        if user_id not in self.user_search_cache:
            return
        if user_id in self.wait_confirm:
            yield event.plain_result("⚠️ 请先回复 是/否 完成当前选择！")
            return

        self.is_querying = True
        # ========== 为当前任务创建独立文件夹 ==========
        task_dir = create_unique_subdir(user_id)
        try:
            res_list = self.user_search_cache[user_id]
            if select_num < 1 or select_num > len(res_list):
                yield event.plain_result(f"⚠️ 序号超出范围，请输入 1~{len(res_list)} 之间的数字")
                remove_dir(task_dir)
                return

            target_item = res_list[select_num - 1]
            title = target_item["title"]
            gallery_url = target_item["detail_url"]

            # 动态抓取全部分页图片链接
            s_links = await self.get_s_links_from_gallery_page(gallery_url)
            if not s_links:
                yield event.plain_result(f"📌 标题：{title}\n🔗 画廊链接：{gallery_url}\n❌ 未抓取到任何图片")
                # 清理空目录
                remove_dir(task_dir)
                return

            total_img = len(s_links)
            estimate_time = round(total_img * PER_IMG_PROCESS_DELAY)

            # 自定义格式提示文案
            tip_text = (
                f"📌 标题：{title}\n"
                f"🔗 画廊链接：{gallery_url}\n"
                f"🖼 图片数量：{total_img}张\n"
                f"🖼 正在下载图片，请稍候...（预计需要{estimate_time}秒）"
            )
            yield event.plain_result(tip_text)

            # 下载图片到当前任务独立目录
            local_png_paths = []
            for idx, s_link in enumerate(s_links, 1):
                img_url = await self.get_original_img_from_s_page(s_link)
                if not img_url:
                    continue
                png_path = await download_and_convert_to_png(img_url, idx, task_dir)
                if png_path and os.path.exists(png_path):
                    local_png_paths.append(png_path)
                await asyncio.sleep(0.2)

            if not local_png_paths:
                yield event.plain_result("❌ 所有图片下载/转换失败")
                remove_dir(task_dir)
                return

            # 存储：图片列表 + 任务目录
            self.wait_confirm[user_id] = (local_png_paths, task_dir)
            confirm_tip = (
                f"✅ 图片准备完成，总计 {len(local_png_paths)} 张\n"
                "请回复【是】发送图片 / 回复【否】取消本次发送"
            )
            yield event.plain_result(confirm_tip)
        finally:
            self.is_querying = False

    # 监听 是/否 二次确认
    @filter.regex(CONFIRM_REG)
    async def handle_confirm(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        msg = event.message_str.strip()

        if user_id not in self.wait_confirm:
            return

        # 取出：图片路径列表 + 任务专属目录
        png_path_list, task_dir = self.wait_confirm[user_id]
        total_img = len(png_path_list)

        # 回复 否：取消发送 → 直接清理当前任务目录
        if msg in ("否", "不", "no"):
            yield event.plain_result("✅ 已取消本次图片发送，可重新发起搜索。")
            del self.wait_confirm[user_id]
            remove_dir(task_dir)
            if user_id in self.user_search_cache:
                del self.user_search_cache[user_id]
            return

        # 回复 是：发送图片 + 结束后清理目录
        if msg in ("是", "对", "yes"):
            yield event.plain_result("🚀 开始发送图片")
            del self.wait_confirm[user_id]

            fail_img_list = []
            # 第一轮全量发送
            logger.info("[图片发送] 开始第一轮全量发送")
            for idx, png_path in enumerate(png_path_list, 1):
                if not os.path.exists(png_path):
                    logger.warning(f"[文件缺失] 第{idx}张 {png_path} 跳过")
                    await asyncio.sleep(SINGLE_IMG_SEND_DELAY)
                    continue

                send_success = False
                for try_cnt in range(1, SEND_TRY_PER_IMG + 1):
                    try:
                        yield event.chain_result([Image.fromFileSystem(png_path)])
                        logger.info(f"[发送成功] 第{idx}/{total_img}张 | 尝试次数:{try_cnt}")
                        send_success = True
                        break
                    except Exception as e:
                        logger.warning(f"[发送失败] 第{idx}张 | 尝试次数:{try_cnt} | 错误:{str(e)}")
                        await asyncio.sleep(SINGLE_IMG_SEND_DELAY)

                if not send_success:
                    fail_img_list.append((idx, png_path))
                    logger.error(f"[单次轮次彻底失败] 第{idx}张 加入重发队列")

                await asyncio.sleep(SINGLE_IMG_SEND_DELAY)

            # 轮回重发失败图片
            if fail_img_list:
                logger.info(f"[第一轮结束] 共 {len(fail_img_list)} 张图片发送失败，开始轮回重发")
                for round_num in range(2, MAX_SEND_ROUND + 1):
                    if not fail_img_list:
                        logger.info(f"[第{round_num}轮] 无失败图片，提前结束重发")
                        break
                    logger.info(f"[开始第{round_num}轮重发] 待重发数量: {len(fail_img_list)}")
                    new_fail_list = []
                    for idx, png_path in fail_img_list:
                        send_success = False
                        for try_cnt in range(1, SEND_TRY_PER_IMG + 1):
                            try:
                                yield event.chain_result([Image.fromFileSystem(png_path)])
                                logger.info(f"[重发成功] 第{idx}张 | 轮次:{round_num} | 尝试:{try_cnt}")
                                send_success = True
                                break
                            except Exception as e:
                                logger.warning(f"[重发失败] 第{idx}张 | 轮次:{round_num} | 尝试:{try_cnt}")
                            # ===================== 【修复笔误1】 =====================
                            # 原代码：await asyncio.sleep(SINGLE_IMG_DELAY) 变量不存在
                            await asyncio.sleep(SINGLE_IMG_SEND_DELAY)

                        if not send_success:
                            new_fail_list.append((idx, png_path))
                        await asyncio.sleep(SINGLE_IMG_SEND_DELAY)
                    fail_img_list = new_fail_list

                if fail_img_list:
                    # ===================== 【修复笔误2】 =====================
                    # 原代码：len(fail_img) 变量未定义，改为 fail_img_list
                    yield event.plain_result(f"🎉 图片发送完毕，{len(fail_img_list)}张图片多次超时未能发送")
                else:
                    yield event.plain_result("🎉 所有图片全部发送成功！")
            else:
                yield event.plain_result("🎉 所有图片发送完毕！")

            # 发送完成后清理当前任务独立文件夹
            remove_dir(task_dir)
            if user_id in self.user_search_cache:
                del self.user_search_cache[user_id]
            return

        yield event.plain_result("❓ 请仅回复【是】或【否】，请重新选择！")

    async def terminate(self):
        pass
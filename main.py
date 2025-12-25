import io
import re
from typing import Optional

import aiohttp
from PIL import Image

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Plain, Image as ImageComponent
from astrbot.core.message.message_event_result import MessageChain

try:
    from spankbang_api import Client
except ImportError:
    Client = None


@register("astrbot_plugin_spankbang", "YourName", "SpankBang 视频搜索插件，支持搜索视频并返回打码封面", "1.0.0")
class SpankBangPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.client: Optional[Client] = None

    async def initialize(self):
        """插件初始化方法"""
        if Client is None:
            logger.warning("spankbang_api 库未安装，请运行: pip install spankbang_api")
            return
        
        try:
            # 获取全局配置中的代理设置
            global_config = self.context.get_config(umo='global')
            proxy = global_config.get('proxy', '')
            
            self.client = Client()
            
            # 如果配置了代理，设置到 session
            if proxy:
                self.client.core.config.proxy = proxy
                logger.info(f"SpankBang 插件初始化成功，使用代理: {proxy}")
            else:
                logger.info("SpankBang 插件初始化成功")
        except Exception as e:
            logger.error(f"SpankBang 插件初始化失败: {e}")

    async def terminate(self):
        """插件销毁方法"""
        if self.client:
            self.client = None
        logger.info("SpankBang 插件已终止")

    def _get_config(self, event: AstrMessageEvent) -> dict:
        """获取插件配置"""
        config = self.context.get_config(umo=event.unified_msg_origin)
        return {
            'mosaic_level': config.get('mosaic_level', 50),
            'max_results': config.get('max_results', 5),
            'enable_mosaic': config.get('enable_mosaic', True),
            'proxy': config.get('proxy', '')
        }

    async def _download_image(self, url: str, proxy: str = "") -> Optional[Image.Image]:
        """下载图片"""
        try:
            connector = None
            if proxy:
                # 根据代理类型创建连接器
                if proxy.startswith('socks'):
                    import aiohttp_socks
                    connector = aiohttp_socks.ProxyConnector.from_url(proxy)
                else:
                    connector = aiohttp.TCPConnector()
            
            async with aiohttp.ClientSession(connector=connector) as session:
                kwargs = {'timeout': aiohttp.ClientTimeout(total=30)}
                if proxy and not proxy.startswith('socks'):
                    kwargs['proxy'] = proxy
                
                async with session.get(url, **kwargs) as response:
                    if response.status == 200:
                        image_data = await response.read()
                        return Image.open(io.BytesIO(image_data))
        except Exception as e:
            logger.error(f"下载图片失败: {e}")
        return None

    def _apply_mosaic(self, image: Image.Image, level: int) -> Image.Image:
        """应用马赛克效果"""
        if level <= 0:
            return image
        
        # 计算马赛克块大小 (level 0-100 映射到 1-50)
        block_size = max(1, int(level / 2))
        
        # 缩小图片
        small = image.resize(
            (image.width // block_size, image.height // block_size),
            Image.Resampling.NEAREST
        )
        
        # 放大回原尺寸
        mosaic = small.resize(
            (image.width, image.height),
            Image.Resampling.NEAREST
        )
        
        return mosaic

    async def _process_thumbnail(self, thumbnail_url: str, config: dict) -> Optional[str]:
        """处理缩略图，应用打码效果"""
        if not config.get('enable_mosaic', True):
            return thumbnail_url
        
        proxy = config.get('proxy', '')
        image = await self._download_image(thumbnail_url, proxy)
        if image is None:
            return thumbnail_url
        
        mosaic_level = config.get('mosaic_level', 50)
        mosaic_image = self._apply_mosaic(image, mosaic_level)
        
        # 保存到临时文件
        from astrbot.core.utils.io import save_temp_img
        return save_temp_img(mosaic_image)

    @filter.command("sb_search", alias={"sb", "spankbang"})
    async def search_videos(self, event: AstrMessageEvent):
        """搜索 SpankBang 视频
        
        用法: /sb_search <关键词> [页数] [筛选] [画质] [时长] [日期]
        示例: /sb_search keyword 1 new hd 10 d
        
        参数说明:
        - 筛选: trending(热门), new(最新), featured(精选), popular(流行)
        - 画质: hd(720p), fhd(1080p), uhd(4k)
        - 时长: 10(10分钟), 20(20分钟), 40(40分钟以上)
        - 日期: d(今天), w(本周), m(本月), y(今年)
        """
        if Client is None:
            yield event.plain_result("spankbang_api 库未安装，请联系管理员安装")
            return
        
        message_str = event.message_str.strip()
        parts = message_str.split(None, 6)
        
        if len(parts) < 2:
            yield event.plain_result("用法: /sb_search <关键词> [页数] [筛选] [画质] [时长] [日期]\n示例: /sb_search keyword 1 new hd 10 d")
            return
        
        query = parts[1]
        pages = 1
        filter_type = None
        quality = ""
        duration = ""
        date = ""
        
        # 解析可选参数
        if len(parts) > 2 and parts[2].isdigit():
            pages = int(parts[2])
        if len(parts) > 3:
            filter_type = parts[3].lower()
            if filter_type not in ["trending", "new", "featured", "popular"]:
                filter_type = None
        if len(parts) > 4:
            quality = parts[4].lower()
            if quality not in ["hd", "fhd", "uhd"]:
                quality = ""
        if len(parts) > 5:
            duration = parts[5]
            if duration not in ["10", "20", "40"]:
                duration = ""
        if len(parts) > 6:
            date = parts[6].lower()
            if date not in ["d", "w", "m", "y"]:
                date = ""
        
        config = self._get_config(event)
        max_results = config.get('max_results', 5)
        
        try:
            yield event.plain_result(f"正在搜索: {query}...")
            
            # 搜索视频
            results = []
            for video in self.client.search(
                query,
                filter=filter_type,
                quality=quality,
                duration=duration,
                date=date,
                pages=pages
            ):
                if len(results) >= max_results:
                    break
                results.append(video)
            
            if not results:
                yield event.plain_result("未找到相关视频")
                return
            
            # 构建响应消息
            chain = []
            
            # 添加搜索结果标题
            chain.append(Plain(f"🔍 搜索结果: {query}\n\u200E"))
            
            # 显示搜索条件
            conditions = []
            if filter_type:
                filter_map = {"trending": "热门", "new": "最新", "featured": "精选", "popular": "流行"}
                conditions.append(f"筛选: {filter_map.get(filter_type, filter_type)}")
            if quality:
                quality_map = {"hd": "720p", "fhd": "1080p", "uhd": "4K"}
                conditions.append(f"画质: {quality_map.get(quality, quality)}")
            if duration:
                duration_map = {"10": "10分钟", "20": "20分钟", "40": "40分钟+"}
                conditions.append(f"时长: {duration_map.get(duration, duration)}")
            if date:
                date_map = {"d": "今天", "w": "本周", "m": "本月", "y": "今年"}
                conditions.append(f"日期: {date_map.get(date, date)}")
            
            if conditions:
                chain.append(Plain(f"条件: {' | '.join(conditions)}\n\u200E"))
            
            chain.append(Plain(f"共找到 {len(results)} 个结果\n\n\u200E"))
            
            for idx, video in enumerate(results, 1):
                title = getattr(video, 'title', '未知标题')
                thumbnail = getattr(video, 'thumbnail', '')
                length = getattr(video, 'length', '未知时长')
                rating = getattr(video, 'rating', '未知评分')
                author = getattr(video, 'author', '未知作者')
                video_url = getattr(video, 'url', '')
                
                chain.append(Plain(f"【{idx}】{title}\n\u200E"))
                chain.append(Plain(f"⏱️ 时长: {length} | ⭐ 评分: {rating}\n\u200E"))
                chain.append(Plain(f"👤 作者: {author}\n\u200E"))
                if video_url:
                    chain.append(Plain(f"🔗 链接: {video_url}\n\u200E"))
                
                # 处理缩略图
                if thumbnail:
                    processed_thumbnail = await self._process_thumbnail(thumbnail, config)
                    if processed_thumbnail:
                        chain.append(ImageComponent.fromFileSystem(processed_thumbnail))
                
                chain.append(Plain("\n\u200E"))
            
            yield event.chain_result(chain)
            
        except Exception as e:
            logger.error(f"搜索视频失败: {e}")
            yield event.plain_result(f"搜索失败: {str(e)}")

    @filter.command("sb_video")
    async def get_video_info(self, event: AstrMessageEvent):
        """获取视频详细信息
        
        用法: /sb_video <视频ID>
        示例: /sb_video 95s5u
        """
        if Client is None:
            yield event.plain_result("spankbang_api 库未安装，请联系管理员安装")
            return
        
        message_str = event.message_str.strip()
        parts = message_str.split(None, 1)
        
        if len(parts) < 2:
            yield event.plain_result("用法: /sb_video <视频ID>\n示例: /sb_video 95s5u")
            return
        
        video_id = parts[1].strip()
        
        # 构建视频URL
        url = f"https://spankbang.com/{video_id}/video/"
        
        config = self._get_config(event)
        
        try:
            yield event.plain_result("正在获取视频信息...")
            
            video = self.client.get_video(url)
            
            title = getattr(video, 'title', '未知标题')
            description = getattr(video, 'description', '无描述')
            thumbnail = getattr(video, 'thumbnail', '')
            length = getattr(video, 'length', '未知时长')
            rating = getattr(video, 'rating', '未知评分')
            author = getattr(video, 'author', '未知作者')
            tags = getattr(video, 'tags', [])
            qualities = getattr(video, 'video_qualities', [])
            
            # 构建响应消息
            chain = []
            
            chain.append(Plain("📹 视频信息\n\u200E"))
            chain.append(Plain(f"{'='*30}\n\u200E"))
            chain.append(Plain(f"📌 标题: {title}\n\u200E"))
            chain.append(Plain(f"⏱️ 时长: {length}\n\u200E"))
            chain.append(Plain(f"⭐ 评分: {rating}\n\u200E"))
            chain.append(Plain(f"👤 作者: {author}\n\u200E"))
            chain.append(Plain(f"🔗 链接: {url}\n\u200E"))
            chain.append(Plain(f"🎬 可用画质: {', '.join(qualities) if qualities else '未知'}\n\u200E"))
            
            if tags:
                chain.append(Plain(f"🏷️ 标签: {', '.join(tags[:10])}\n\u200E"))
            
            if description:
                chain.append(Plain(f"\n📝 描述:\n{description[:200]}...\n\u200E"))
            
            # 处理缩略图
            if thumbnail:
                processed_thumbnail = await self._process_thumbnail(thumbnail, config)
                if processed_thumbnail:
                    chain.append(ImageComponent.fromFileSystem(processed_thumbnail))
            
            yield event.chain_result(chain)
            
        except Exception as e:
            logger.error(f"获取视频信息失败: {e}")
            yield event.plain_result(f"获取视频信息失败: {str(e)}")

    @filter.command("sb_channel")
    async def get_channel_info(self, event: AstrMessageEvent):
        """获取频道信息
        
        用法: /sb_channel <频道ID>
        示例: /sb_channel xxx
        """
        if Client is None:
            yield event.plain_result("spankbang_api 库未安装，请联系管理员安装")
            return
        
        message_str = event.message_str.strip()
        parts = message_str.split(None, 1)
        
        if len(parts) < 2:
            yield event.plain_result("用法: /sb_channel <频道ID>\n示例: /sb_channel xxx")
            return
        
        channel_id = parts[1].strip()
        
        # 构建频道URL
        url = f"https://spankbang.com/channel/{channel_id}/"
        
        config = self._get_config(event)
        
        try:
            yield event.plain_result("正在获取频道信息...")
            
            channel = self.client.get_channel(url)
            
            name = getattr(channel, 'name', '未知频道')
            video_count = getattr(channel, 'video_count', '0')
            views_count = getattr(channel, 'views_count', '0')
            subscribers_count = getattr(channel, 'subscribers_count', '0')
            image = getattr(channel, 'image', '')
            
            # 构建响应消息
            chain = []
            
            chain.append(Plain("📺 频道信息\n\u200E"))
            chain.append(Plain(f"{'='*30}\n\u200E"))
            chain.append(Plain(f"📌 名称: {name}\n\u200E"))
            chain.append(Plain(f"🎬 视频数: {video_count}\n\u200E"))
            chain.append(Plain(f"👁️ 观看数: {views_count}\n\u200E"))
            chain.append(Plain(f"👥 订阅数: {subscribers_count}\n\u200E"))
            chain.append(Plain(f"🔗 链接: {url}\n\u200E"))
            
            # 处理封面图
            if image:
                processed_image = await self._process_thumbnail(image, config)
                if processed_image:
                    chain.append(ImageComponent.fromFileSystem(processed_image))
            
            yield event.chain_result(chain)
            
        except Exception as e:
            logger.error(f"获取频道信息失败: {e}")
            yield event.plain_result(f"获取频道信息失败: {str(e)}")

    @filter.command("sb_pornstar")
    async def get_pornstar_info(self, event: AstrMessageEvent):
        """获取演员信息
        
        用法: /sb_pornstar <演员ID>
        示例: /sb_pornstar xxx
        """
        if Client is None:
            yield event.plain_result("spankbang_api 库未安装，请联系管理员安装")
            return
        
        message_str = event.message_str.strip()
        parts = message_str.split(None, 1)
        
        if len(parts) < 2:
            yield event.plain_result("用法: /sb_pornstar <演员ID>\n示例: /sb_pornstar xxx")
            return
        
        pornstar_id = parts[1].strip()
        
        # 构建演员URL
        url = f"https://spankbang.com/pornstar/{pornstar_id}/"
        
        config = self._get_config(event)
        
        try:
            yield event.plain_result("正在获取演员信息...")
            
            pornstar = self.client.get_pornstar(url)
            
            name = getattr(pornstar, 'name', '未知演员')
            video_count = getattr(pornstar, 'video_count', '0')
            views_count = getattr(pornstar, 'views_count', '0')
            subscribers_count = getattr(pornstar, 'subscribers_count', '0')
            image = getattr(pornstar, 'image', '')
            
            # 构建响应消息
            chain = []
            
            chain.append(Plain("⭐ 演员信息\n\u200E"))
            chain.append(Plain(f"{'='*30}\n\u200E"))
            chain.append(Plain(f"📌 姓名: {name}\n\u200E"))
            chain.append(Plain(f"🎬 视频数: {video_count}\n\u200E"))
            chain.append(Plain(f"👁️ 观看数: {views_count}\n\u200E"))
            chain.append(Plain(f"👥 粉丝数: {subscribers_count}\n\u200E"))
            chain.append(Plain(f"🔗 链接: {url}\n\u200E"))
            
            # 处理头像
            if image:
                processed_image = await self._process_thumbnail(image, config)
                if processed_image:
                    chain.append(ImageComponent.fromFileSystem(processed_image))
            
            yield event.chain_result(chain)
            
        except Exception as e:
            logger.error(f"获取演员信息失败: {e}")
            yield event.plain_result(f"获取演员信息失败: {str(e)}")

    @filter.command("sb_creator")
    async def get_creator_info(self, event: AstrMessageEvent):
        """获取创作者信息
        
        用法: /sb_creator <创作者ID>
        示例: /sb_creator xxx
        """
        if Client is None:
            yield event.plain_result("spankbang_api 库未安装，请联系管理员安装")
            return
        
        message_str = event.message_str.strip()
        parts = message_str.split(None, 1)
        
        if len(parts) < 2:
            yield event.plain_result("用法: /sb_creator <创作者ID>\n示例: /sb_creator xxx")
            return
        
        creator_id = parts[1].strip()
        
        # 构建创作者URL
        url = f"https://spankbang.com/creator/{creator_id}/"
        
        config = self._get_config(event)
        
        try:
            yield event.plain_result("正在获取创作者信息...")
            
            creator = self.client.get_creator(url)
            
            name = getattr(creator, 'name', '未知创作者')
            video_count = getattr(creator, 'video_count', '0')
            views_count = getattr(creator, 'views_count', '0')
            subscribers_count = getattr(creator, 'subscribers_count', '0')
            image = getattr(creator, 'image', '')
            
            # 构建响应消息
            chain = []
            
            chain.append(Plain("🎨 创作者信息\n\u200E"))
            chain.append(Plain(f"{'='*30}\n\u200E"))
            chain.append(Plain(f"📌 名称: {name}\n\u200E"))
            chain.append(Plain(f"🎬 视频数: {video_count}\n\u200E"))
            chain.append(Plain(f"👁️ 观看数: {views_count}\n\u200E"))
            chain.append(Plain(f"👥 订阅数: {subscribers_count}\n\u200E"))
            chain.append(Plain(f"🔗 链接: {url}\n\u200E"))
            
            # 处理头像
            if image:
                processed_image = await self._process_thumbnail(image, config)
                if processed_image:
                    chain.append(ImageComponent.fromFileSystem(processed_image))
            
            yield event.chain_result(chain)
            
        except Exception as e:
            logger.error(f"获取创作者信息失败: {e}")
            yield event.plain_result(f"获取创作者信息失败: {str(e)}")

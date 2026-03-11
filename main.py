import os
import json
import time
from typing import List, Optional
from astrbot.core import AstrBotConfig
from astrbot.core.provider.func_tool import ProviderManager
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.message.components import Plain, At
from astrbot.core import logger

class GroupFilterPlugin:
    def __init__(self, config: AstrBotConfig, provider_manager: ProviderManager):
        self.provider_manager = provider_manager
        self.config_data = config  # 配置对象，可直接通过字典方式访问
        self.monitor_groups = self._parse_group_ids(config.get("monitor_groups", ""))
        self.filter_prompt = config.get("filter_prompt", "")
        self.violation_message = config.get("violation_message", "")

    def _parse_group_ids(self, group_str: str) -> List[str]:
        """将配置中的逗号分隔字符串转为列表"""
        if not group_str:
            return []
        return [g.strip() for g in group_str.split(",") if g.strip()]

    async def initialize(self):
        """插件初始化时调用"""
        logger.info(f"群聊过滤器插件初始化完成，监控群组: {self.monitor_groups}")

    async def on_message(self, event: AstrMessageEvent):
        """核心消息处理入口"""
        # 1. 提取群ID
        group_id = self._extract_group_id(event)
        if not group_id:
            return  # 非群消息

        # 2. 白名单检查
        if group_id not in self.monitor_groups:
            return

        # 3. 获取消息文本
        message_text = event.get_message_str()

        # 4. 调用AI判断
        judgment = await self._judge_with_ai(message_text)

        # 5. 若违规，处理撤回和警告
        if judgment == "filtered":
            await self._recall_message(event, group_id)
            await self._send_warning(event, group_id)
            logger.info(f"已处理违规消息: 群={group_id}, 用户={event.get_sender_id()}")

    def _extract_group_id(self, event: AstrMessageEvent) -> Optional[str]:
        """根据适配器类型提取群ID"""
        # 方法1: 直接从event属性获取
        if hasattr(event, 'group_id') and event.group_id:
            return str(event.group_id)

        # 方法2: 从message_obj中获取（适用于aiocqhttp）
        if hasattr(event, 'message_obj') and hasattr(event.message_obj, 'group_id'):
            return str(event.message_obj.group_id)

        # 方法3: 从原始字典解析（备用）
        try:
            raw = event.get_extra("raw_message")
            if raw and 'group_id' in raw:
                return str(raw['group_id'])
        except:
            pass
        return None

    async def _judge_with_ai(self, message: str) -> str:
        """调用模型判断消息是否违规"""
        # 构建完整提示词，可考虑将配置文件要求动态插入
        full_prompt = f"{self.filter_prompt}\n消息内容：{message}"

        # 获取默认文本模型提供商
        provider = self.provider_manager.get_provider()
        if not provider:
            logger.error("未配置模型提供商，跳过审核")
            return "none"

        try:
            response = await provider.text_chat(
                prompt=full_prompt,
                session_id=None,      # 无上下文
                contexts=[],
                system_prompt=None
            )
            # 清理并解析结果
            result = response.strip().lower()
            if "<filtered>" in result:
                return "filtered"
            elif "<none>" in result:
                return "none"
            else:
                logger.warning(f"AI返回格式异常: {result}")
                return "none"
        except Exception as e:
            logger.error(f"AI调用失败: {e}")
            return "none"  # 异常时放行

    async def _recall_message(self, event: AstrMessageEvent, group_id: str):
        """调用OneBot v11撤回消息"""
        try:
            # 获取消息ID，需转换为整数
            message_id = event.get_extra("message_id")
            if not message_id:
                # 尝试从message_obj获取
                if hasattr(event, 'message_obj') and hasattr(event.message_obj, 'message_id'):
                    message_id = event.message_obj.message_id
                else:
                    logger.error("无法获取消息ID")
                    return

            # 注意：NapCat要求message_id为整数
            recall_result = await event.bot.api.call_action(
                action="delete_msg",
                params={"message_id": int(message_id)}
            )
            if recall_result:
                logger.debug(f"消息撤回成功: {message_id}")
            else:
                logger.error(f"消息撤回失败: {message_id}")
        except ValueError:
            logger.error(f"消息ID无法转换为整数: {message_id}")
        except Exception as e:
            logger.error(f"撤回消息异常: {e}")

    async def _send_warning(self, event: AstrMessageEvent, group_id: str):
        """在群内发送@警告"""
        sender_id = event.get_sender_id()
        # 构造消息链：At + 空格 + 固定文本
        warning_chain = MessageChain([
            At(qq=sender_id),
            Plain(text=" " + self.violation_message)
        ])
        await event.send(warning_chain)

    async def on_plugin_stop(self):
        """插件停止时清理"""
        logger.info("群聊过滤器插件已停止")
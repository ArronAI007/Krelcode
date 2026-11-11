"""具体 provider 适配器。每个 adapter 独占一种 wire format，
负责：规范化消息 ⇄ wire format 互转、SSE 事件翻译、finish_reason/usage 规范化。
"""

#!/usr/bin/env python3
"""本地假模型：只为联调。返回一篇固定的合规稿件。

单独跑：python tools/mock_llm.py 8898
然后在工作台「设置」里把 Base URL 填 http://127.0.0.1:8898/v1，模型名随便，Key 随便。
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

ARTICLE = {
    "title": "秤和估数，哪个算数？",
    "digest": "果农说应该有一千多斤吧，通报说称出181斤。差的那八百斤不是苹果，是没人打个招呼这件事。",
    "keywords": ["直播带货", "私闯果园", "现场称量", "消费维权", "行业乱象",
                 "流量经济", "农产品", "短视频", "赔偿标准", "边界感"],
    "markdown": """主播进了果园，苹果掉了满地，切开的一半一半摊在土上。**通报写的是181斤**，果农视频里说的是那句带「吧」字的「应该有一千多斤吧」。

## 秤是称出来的，估数是猜出来的

现场在双方见证下过的秤，是181斤。视频里那句「应该有一千多斤吧」，尾音那个「吧」，是估的。

这两件事本来不该被放在一起比。可舆论偏偏就比起来了——因为没人说清，到底哪个数才算数。

**181斤按3元一斤是543元，主播主动报的6000元，是11倍。**

所以果农不接受，未必是嫌钱少。换个位置想：你家地里进了人，苹果被切开摆拍，最后给你一笔钱，说这事完了。你要的可能不是钱，是一句「我不该没打招呼就进来」。

## 通报没解释的那个动作

通报写了双方达成和解，没写赔偿金额。但它也没解释另一件事：账号在22日19时下架了所有视频。

赔偿解决的是这一块地。清空作品解决的，是别的东西。

这里的分寸值得琢磨。一个人愿意赔钱，说明他认了这一块的账；一个人连夜清空，说明他意识到了账可能不止这一块。

> 算法的世界里，只有切开的断面和流出来的汁水是有画面的。「这是谁家的地」不在画面里，所以它不算内容。

这也是流量经济里反复出现的错位：**可拍摄的部分被放大，不可拍摄的部分被抹掉。**确认、打招呼、征得同意，这些动作都不产出画面，于是它们在流水线上最先被省掉。

## 普通人能拿走什么

第一，别把「我以为」当成许可。走错地点、认错果园，都是主观的；门是不是你推开的，是客观的。

第二，遇到侵权先固定证据再谈钱。称重、录像、找见证人，把「估」变成「称」。

第三，别急着替任何一方下结论。通报写了什么就读什么，没写的部分，留一个问号比补一段想象更稳妥。

这件事真正的分歧点不在181和1000之间，而在**赔偿能不能买断「没打招呼」这件事**。你觉得能吗？评论区说说你家的地被人进过没有。

#直播带货 #私闯果园 #现场称量 #消费维权 #流量经济 #农产品 #短视频 #边界感""",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静音
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        content = json.dumps(ARTICLE, ensure_ascii=False)
        body = json.dumps({
            "id": "mock-1",
            "object": "chat.completion",
            "model": "mock",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8898
    print(f"mock LLM on http://127.0.0.1:{port}/v1", flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()

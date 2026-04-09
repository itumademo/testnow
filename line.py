"""
LINE通知モジュール

LINE Messaging APIの「ブロードキャスト」機能を使用。
友だち追加済みの全ユーザーにメッセージを送信する。
→ ユーザーIDの取得が不要。友だち追加するだけでOK。

セットアップ手順:
1. https://developers.line.biz/ でMessaging APIチャネルを作成
2. チャネルアクセストークン（長期）を発行
3. 公式アカウントを自分のLINEで友だち追加
4. 環境変数に設定:
   - LINE_CHANNEL_TOKEN: チャネルアクセストークン
   （LINE_USER_IDは不要）
"""

import httpx
from src.config import LINE_CHANNEL_TOKEN


class LINENotifier:
    """LINE Messaging API経由でメッセージ送信（ブロードキャスト方式）"""

    # ブロードキャスト = 友だち全員に送信（USER_ID不要）
    API_URL = "https://api.line.me/v2/bot/message/broadcast"
    MAX_MESSAGE_LENGTH = 5000  # LINEの1メッセージ上限

    def __init__(self, token: str = ""):
        self.token = token or LINE_CHANNEL_TOKEN
        self.client = httpx.Client(timeout=30)

    def send(self, text: str) -> bool:
        """
        テキストメッセージを友だち全員に送信

        長文は自動的に分割して複数メッセージで送信する（最大5通）
        """
        if not self.token:
            print("[LINE] ⚠️ TOKEN not set. Printing to stdout instead.")
            print("=" * 50)
            print(text)
            print("=" * 50)
            return False

        # 長文分割
        chunks = self._split_message(text)

        for i, chunk in enumerate(chunks[:5]):  # 最大5通
            messages = [{"type": "text", "text": chunk}]
            payload = {
                "messages": messages,
            }
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            }

            try:
                resp = self.client.post(self.API_URL, json=payload, headers=headers)
                if resp.status_code == 200:
                    print(f"[LINE] ✅ Message {i+1}/{len(chunks)} sent successfully")
                else:
                    print(f"[LINE] ❌ Error {resp.status_code}: {resp.text}")
                    return False
            except Exception as e:
                print(f"[LINE] ❌ Exception: {e}")
                return False

        return True

    def _split_message(self, text: str) -> list[str]:
        """メッセージをLINEの上限に合わせて分割"""
        if len(text) <= self.MAX_MESSAGE_LENGTH:
            return [text]

        chunks = []
        lines = text.split("\n")
        current = ""

        for line in lines:
            if len(current) + len(line) + 1 > self.MAX_MESSAGE_LENGTH:
                if current:
                    chunks.append(current)
                current = line
            else:
                current = current + "\n" + line if current else line

        if current:
            chunks.append(current)

        return chunks

    def close(self):
        self.client.close()

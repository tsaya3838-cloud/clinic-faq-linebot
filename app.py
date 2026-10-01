import logging
import os
import re

import requests
from flask import Flask, abort, request
from dotenv import load_dotenv
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
DIFY_API_KEY = os.getenv("DIFY_API_KEY")
DIFY_API_ENDPOINT = "https://api.dify.ai/v1/chat-messages"

if not LINE_CHANNEL_ACCESS_TOKEN:
    raise ValueError("LINE_CHANNEL_ACCESS_TOKEN is not set.")
if not LINE_CHANNEL_SECRET:
    raise ValueError("LINE_CHANNEL_SECRET is not set.")
if not DIFY_API_KEY:
    raise ValueError("DIFY_API_KEY is not set.")

handler = WebhookHandler(LINE_CHANNEL_SECRET)

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
api_client = ApiClient(configuration)
line_bot_api = MessagingApi(api_client)

PHONE_GUIDE = "お電話（098-000-0000／平日8:30〜17:00、土曜8:30〜12:00）でお問い合わせください。"

# 緊急性が高い語句。含まれていたら AI を介さず固定文で案内する。
EMERGENCY_KEYWORDS = [
    "胸が痛",
    "胸の痛み",
    "息ができ",
    "息が苦し",
    "息苦し",
    "呼吸が苦し",
    "意識がな",
    "意識が朦朧",
    "意識がもうろう",
    "大量出血",
    "血が止まらな",
    "けいれん",
    "痙攣",
    "倒れ",
    "心臓が痛",
    "呂律が回ら",
    "死にそう",
    "死にたい",
]

EMERGENCY_MESSAGE = (
    "【緊急のご案内】\n"
    "命に関わる可能性があります。ただちに119番に電話するか、救急外来を受診してください。\n"
    "当院では救急対応ができません。\n"
    "ご自身で動けない場合は、周りの方に助けを求めてください。"
)

# 利用者ごとの Dify 会話 ID（文脈の継続用）。試作のためメモリ保持で、再起動すると消える。
conversation_ids: dict[str, str] = {}


def is_emergency(text: str) -> bool:
    return any(keyword in text for keyword in EMERGENCY_KEYWORDS)


def to_plain_text(text: str) -> str:
    # LINE は Markdown を描画しないため、強調記号を除き、行頭の箇条書きを「・」に置き換える。
    text = text.replace("**", "")
    return re.sub(r"^[ \t]*[*-][ \t]+", "・", text, flags=re.MULTILINE)


def call_dify_chat_api(user_text: str, user_id: str) -> str:
    headers = {
        "Authorization": f"Bearer {DIFY_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "inputs": {},
        "query": user_text,
        "response_mode": "blocking",
        "conversation_id": conversation_ids.get(user_id, ""),
        "user": user_id,
    }

    try:
        response = requests.post(
            DIFY_API_ENDPOINT, headers=headers, json=payload, timeout=30
        )
    except requests.RequestException:
        logger.exception("Failed to connect to Dify API")
        return f"現在、自動応答をご利用いただけません。{PHONE_GUIDE}"

    if response.status_code != 200:
        # 会話 ID が無効になった場合に備えて破棄する。本文は患者情報を含みうるためログに残さない。
        conversation_ids.pop(user_id, None)
        logger.error("Dify API error. status=%s", response.status_code)
        return f"現在、応答を取得できませんでした。{PHONE_GUIDE}"

    try:
        data = response.json()
    except ValueError:
        logger.error("Invalid JSON from Dify API")
        return f"応答の解析に失敗しました。{PHONE_GUIDE}"

    answer = data.get("answer")
    if not answer:
        logger.error("Dify response missing answer field")
        return f"応答が空でした。{PHONE_GUIDE}"

    conversation_id = data.get("conversation_id")
    if conversation_id:
        conversation_ids[user_id] = conversation_id

    return to_plain_text(answer)


@app.route("/callback", methods=["POST"])
def callback() -> str:
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    # 患者の発言が含まれるため、本文はログに出力しない。

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.warning("Invalid LINE signature.")
        abort(400)

    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event: MessageEvent) -> None:
    user_text = event.message.text
    user_id = event.source.user_id if event.source and event.source.user_id else "unknown"

    if is_emergency(user_text):
        logger.info("Emergency keyword detected.")
        reply_text = EMERGENCY_MESSAGE
    else:
        reply_text = call_dify_chat_api(user_text=user_text, user_id=user_id)

    line_bot_api.reply_message(
        ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text=reply_text)],
        )
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)

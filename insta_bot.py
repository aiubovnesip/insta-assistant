import os
import hmac
import hashlib
import json
from flask import Flask, request, jsonify, abort
import requests
import openai

# --- Настройки (берутся из переменных окружения на Render) ---
PAGE_ACCESS_TOKEN = os.environ.get("META_PAGE_ACCESS_TOKEN", "")
APP_SECRET = os.environ.get("META_APP_SECRET", "")
IG_USER_ID = os.environ.get("IG_USER_ID", "")  # numeric Instagram Business User ID
WEBHOOK_VERIFY_TOKEN = os.environ.get("WEBHOOK_VERIFY_TOKEN", "verify_token_example")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

openai.api_key = OPENAI_API_KEY

app = Flask(__name__)

# --- Helper: verify X-Hub-Signature-256 (recommended) ---
def verify_signature(request):
    signature = request.headers.get("X-Hub-Signature-256")
    if not signature or not APP_SECRET:
        return True  # если секрета нет — пропускаем (только для теста). В prod — строго проверять.
    try:
        sha_name, signature = signature.split('=')
    except Exception:
        return False
    mac = hmac.new(APP_SECRET.encode('utf-8'), msg=request.data, digestmod=hashlib.sha256)
    expected = mac.hexdigest()
    return hmac.compare_digest(expected, signature)

# --- webhook verification endpoint (GET) ---
@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == WEBHOOK_VERIFY_TOKEN:
        return challenge, 200
    return "Verification failed", 403

# --- send message via Messenger API for Instagram ---
def send_instagram_message(recipient_id, text):
    if not PAGE_ACCESS_TOKEN or not IG_USER_ID:
        print("Missing PAGE_ACCESS_TOKEN or IG_USER_ID")
        return None
    url = f"https://graph.facebook.com/v17.0/{IG_USER_ID}/messages"
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text}
    }
    headers = {"Authorization": f"Bearer {PAGE_ACCESS_TOKEN}", "Content-Type": "application/json"}
    r = requests.post(url, headers=headers, json=payload, timeout=10)
    try:
        r.raise_for_status()
    except Exception as e:
        print("Send message error:", r.status_code, r.text)
        return None
    return r.json()

# --- call OpenAI chat to generate salesy answer (simple prompt engineering) ---
def call_openai_chat(user_text, history=None):
    if not OPENAI_API_KEY:
        return "OpenAI API key not set."
    system_prompt = {
        "role": "system",
        "content": (
            "Вы — профессиональный sales-ассистент для Instagram. "
            "Кратко выявляйте потребности, предлагайте релевантное решение и делайте мягкий призыв к действию. "
            "Если нужно — задайте 1-2 уточняющих вопроса. Откликайтесь дружелюбно и уверенно."
        )
    }
    messages = [system_prompt]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    # используем Chat Completions
    resp = openai.ChatCompletion.create(
        model="gpt-4o-mini",   # при необходимости замените на доступную вам модель
        messages=messages,
        temperature=0.2,
        max_tokens=400
    )
    return resp.choices[0].message['content'].strip()

# --- webhook receiver (POST) ---
@app.route("/webhook", methods=["POST"])
def webhook_receive():
    # проверка подписи (рекомендуется)
    if not verify_signature(request):
        print("Invalid signature")
        return abort(403)

    data = request.get_json(force=True)
    # простая отладочная печать
    print("Incoming webhook:", json.dumps(data))

    # обработка формата: Graph API может присылать разные поля; мы ищем messages
    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages") or value.get("message") or []
                # иногда структура отличается — обработаем common case
                if isinstance(messages, dict):
                    messages = [messages]
                for m in messages:
                    # типичное поле: m["from"], m["text"]
                    sender_id = None
                    text = ""
                    if isinstance(m, dict):
                        sender_id = m.get("from", {}).get("id") or m.get("from")
                        # текст может быть в разных полях
                        text = m.get("text") or m.get("message") or ""
                        if not text:
                            # иногда text — внутри m['text']['body']
                            txt_struct = m.get("text") or {}
                            if isinstance(txt_struct, dict):
                                text = txt_struct.get("body", "") or ""
                    if not sender_id:
                        # fallback — в value может быть messages list with "from": "<id>"
                        sender_id = m.get("from")
                    if not text:
                        text = ""
                    if sender_id:
                        # для первого теста — короткий ответ от OpenAI
                        answer = call_openai_chat(text)
                        send_instagram_message(sender_id, answer)
    except Exception as e:
        print("Error handling webhook payload:", e)
    return jsonify(status="ok"), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

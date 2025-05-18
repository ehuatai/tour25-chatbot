from flask import Flask, request, jsonify
import json
import requests
import re
import os
import time
import hashlib
from openai import OpenAI
from dotenv import load_dotenv
from collections import OrderedDict

# Initialize Flask app
app = Flask(__name__)
load_dotenv()

# Use a cache of processed message IDs instead of timestamp comparison
# OrderedDict will help us maintain the order for LRU eviction
PROCESSED_MESSAGE_CACHE = OrderedDict()
MAX_CACHE_SIZE = 100  # Adjust based on expected traffic

PERSONAS = ["brad", "jack"]

def send_message(channel_id, text, persona):
    slack_token = os.getenv("SLACK_TOKEN")
    url = "https://slack.com/api/chat.postMessage"
    headers = {
        "Authorization": f"Bearer {slack_token}",
        "Content-Type": "application/json",
    }
    script_dir = os.path.dirname(os.path.realpath(__file__))
    file_path = os.path.join(script_dir, "img_urls.json")
    with open(file_path, "r") as f:
        urls = json.load(f)
        image = urls[persona]
    payload = {
        "channel": channel_id,
        "text": text,
        "username": persona.capitalize() + "Bot",
        "icon_url": image,
    }
    print(payload)
    try:
        response = requests.post(url, headers=headers, json=payload)
        return response.json()
    except Exception as e:
        print(e)
        return {"ok": False, "error": str(e)}

def fetch_usernames(user_ids):
    """Fetch usernames for a set of user IDs using Slack API."""
    slack_token = os.getenv("SLACK_TOKEN")
    url = "https://slack.com/api/users.info"
    headers = {
        "Authorization": f"Bearer {slack_token}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    usernames = {}
    for user_id in user_ids:
        try:
            response = requests.get(url, headers=headers, params={"user": user_id})
            data = response.json()
            if data.get("ok"):
                profile = data["user"]["profile"]
                # Prefer display_name, fallback to real_name
                name = profile.get("display_name") or profile.get("real_name") or user_id
                usernames[user_id] = name
            else:
                usernames[user_id] = user_id
        except Exception as e:
            print(f"Exception fetching username for {user_id}: {e}")
            usernames[user_id] = user_id
    return usernames

def fetch_last_channel_messages(channel, limit=10):
    slack_token = os.getenv("SLACK_TOKEN")
    url = "https://slack.com/api/conversations.history"
    headers = {
        "Authorization": f"Bearer {slack_token}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    params = {
        "channel": channel,
        "limit": limit,
    }
    try:
        response = requests.get(url, headers=headers, params=params)
        data = response.json()
        if not data.get("ok"):
            print(f"Error fetching channel history: {data}")
            return []
            
        # Include bot messages in the context (removed the filter)
        messages = [
            {"user": msg.get("user", "bot"), "text": msg["text"], "is_bot": bool(msg.get("bot_id"))}
            for msg in data.get("messages", [])
            if "text" in msg  # Only filter out messages without text
        ]
        
        # Reverse to chronological order (oldest first)
        messages = list(reversed(messages))
        
        # Collect user IDs only for human messages
        user_ids = {msg["user"] for msg in messages if not msg.get("is_bot") and msg["user"] != "bot"}
        usernames = fetch_usernames(user_ids)
        
        # Attach username to each message
        for msg in messages:
            if msg.get("is_bot"):
                # Try to extract bot username from the message if available
                bot_username = msg.get("username", "Bot")
                msg["username"] = bot_username
            else:
                msg["username"] = usernames.get(msg["user"], msg["user"])
                
        return messages
    except Exception as e:
        print(f"Exception fetching channel messages: {e}")
        return []

def format_persona_messages(messages):
    # Format each message with clear delimiters
    return "\n".join(
        f"--- message start ---\n{msg}\n--- message end ---" for msg in messages
    )

def format_channel_messages(messages):
    return "\n".join(
        f"--- channel message start ---\n{msg['username']}: {msg['text']}\n--- channel message end ---"
        for msg in messages
    )

def llm_response(persona, channel=None):
    openai_key = os.getenv("OPEN_AI_KEY")
    client = OpenAI(api_key=openai_key)

    script_dir = os.path.dirname(os.path.realpath(__file__))
    file_path = os.path.join(script_dir, "prompts2.json")
    with open(file_path, "r") as f:
        prompts = json.load(f)
        initial_prompt = prompts["system"]
        persona_prompt = prompts[persona]
    messages_path = os.path.join(script_dir, "messages.json")
    with open(messages_path, "r") as f:
        messages = json.load(f)
        persona_messages = messages[persona]  # Now an array of strings

    formatted_persona_messages = format_persona_messages(persona_messages)

    # Fetch and format last 10 channel messages if channel is provided
    formatted_channel_messages = ""
    if channel:
        last_channel_messages = fetch_last_channel_messages(channel, limit=10)
        if last_channel_messages:
            formatted_channel_messages = (
                "\nHere are the last 10 messages from this Slack channel:\n"
                + format_channel_messages(last_channel_messages)
            )

    system_prompt = (
        initial_prompt
        + "Here is the character you will be playing:\n\n"
        + persona_prompt
        + "\nHere are some messages from the person you are impersonating:\n"
        + formatted_persona_messages
        + formatted_channel_messages
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Respond in the voice of this character."},
    ]

    try:
        response = client.chat.completions.create(
            model="gpt-4.1-2025-04-14", messages=messages 
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"Error calling OpenAI API: {e}")
        return f"Sorry, I couldn't generate a response for {persona}."

@app.route("/scan", methods=["POST"])
def slack_events():
    global PROCESSED_MESSAGE_CACHE
    print(f"Request data: {request.json}")
    data = request.json

    # Handle URL verification challenge
    if data.get("type") == "url_verification":
        return data.get("challenge")

    # Handle message events
    if data.get("event", {}).get("type") == "message":
        event = data.get("event", {})
        
        # Create a unique message identifier using properties that should be unique per message
        event_id = event.get("event_id")  # Primary ID if available
        client_msg_id = event.get("client_msg_id")  # Another potential ID
        
        # If no explicit IDs are available, create a composite key
        if not event_id and not client_msg_id:
            # Combine channel, timestamp, and first 20 chars of text for a unique fingerprint
            channel = event.get("channel", "")
            ts = event.get("ts", "")
            text = event.get("text", "")[:20]  # First 20 chars should be enough
            user = event.get("user", "")
            
            # Create a hash of these components
            msg_hash = hashlib.md5(f"{channel}:{ts}:{user}:{text}".encode()).hexdigest()
            composite_id = f"composite_{msg_hash}"
            
            message_id = composite_id
        else:
            message_id = event_id or client_msg_id
        
        print(f"Processing message with ID: {message_id}")
        
        # Check if we've seen this message before
        if message_id in PROCESSED_MESSAGE_CACHE:
            print(f"Skipping already processed message with ID: {message_id}")
            return jsonify({"status": "already processed"})
        
        # Add to processed messages cache
        PROCESSED_MESSAGE_CACHE[message_id] = True
        
        # If cache is too large, remove oldest entries (LRU eviction)
        if len(PROCESSED_MESSAGE_CACHE) > MAX_CACHE_SIZE:
            # Remove oldest 20% of entries
            for _ in range(MAX_CACHE_SIZE // 5):
                PROCESSED_MESSAGE_CACHE.popitem(last=False)
        
        # Skip bot messages to prevent loops (but these ARE included in context)
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            print(f"Skipping bot message for triggering")
            return jsonify({"status": "bot message"})
        
        # Additional check: skip messages with persona bot usernames for triggering
        username = event.get("username", "").lower()
        if any(persona.lower() + "bot" == username for persona in PERSONAS):
            print(f"Skipping message from persona bot: {username}")
            return jsonify({"status": "persona bot message"})

        message_text = event.get("text", "")
        channel = event.get("channel")

        # Only look for explicit !persona invocations in THIS message
        persona_pattern = r"(?:^|\s)!(" + "|".join(re.escape(name) for name in PERSONAS) + r")\b"
        found_personas = list(
            match.group(1).lower()
            for match in re.finditer(persona_pattern, message_text, re.IGNORECASE)
        )
        
        print(f"Found personas in message: {found_personas}")

        # Process personas sequentially to include previous responses in context
        for persona_name in found_personas:
            # Generate and send response for this persona
            response = llm_response(persona_name, channel)
            send_result = send_message(channel, response, persona_name)
            
            # Log the send result
            print(f"Send result for {persona_name}: {send_result}")
            
            # Brief pause to ensure message is delivered before generating next response
            if len(found_personas) > 1:
                time.sleep(1)  # Short delay to ensure message delivery

    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=4444, debug=True)
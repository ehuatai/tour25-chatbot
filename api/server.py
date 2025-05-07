from flask import Flask, request, jsonify
import json
import requests
import re
import os

# Initialize Flask app
app = Flask(__name__)
SLACK_API_URL = 'https://slack.com/api/'
SLACK_TOKEN = 'xoxb-357822232215-8879468246480-uRl7VMrkKMrOPyBLV1Jt0Jit'
OPENAI_URL = 'https://api.openai.com/v1/chat/completions'
OPENAI_KEY = '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'

# Define personas - easy to extend with more later
PERSONAS = ["brad", "jack"]

def send_message(channel_id, text):
    url = "https://slack.com/api/chat.postMessage"
    
    headers = {
        "Authorization": f"Bearer {SLACK_TOKEN}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "channel": channel_id,
        "text": text
    }
    
    response = requests.post(url, headers=headers, json=payload)
    
    return response.json()

@app.route('/scan', methods=['POST'])
def slack_events():
    # Log the request data for debugging
    print(f"Request data: {request.json}")
    data = request.json
    
    # Handle URL verification challenge
    if data.get('type') == 'url_verification':
        return data.get("challenge")
    
    # Handle message events
    if data.get('event', {}).get('type') == 'message':
        event = data.get('event', {})
        
        # Skip bot messages to prevent loops
        if event.get('bot_id') or event.get('subtype') == 'bot_message':
            return jsonify({"status": "ok"})
        
        message_text = event.get('text', '')
        channel = event.get('channel')
        
        # Check for persona commands using regex
        for persona_name in PERSONAS:
            command_pattern = f"!{persona_name}\\b"
            match = re.search(command_pattern, message_text, re.IGNORECASE)
            
            if match:
                # Generate and send response
                response = llm_response(persona_name)
                send_message(channel, response)
                break
    
    # Return a 200 OK response to acknowledge receipt
    return jsonify({"status": "ok"})

def llm_response(persona):
    script_dir = os.path.dirname(os.path.realpath(__file__))
    
    # Join the directory path with the filename
    file_path = os.path.join(script_dir, 'prompts.json')
    with open(file_path, 'r') as f:
        sys_prompt = json.load(f)[persona]
        print(sys_prompt)
        
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENAI_KEY}"
    }
    
    # Set up payload for the API request
    payload = {
        "model": "gpt-4.1-mini-2025-04-14", 
        "messages": [
            {
                "role": "system",
                "content": sys_prompt
            },
            {
                "role": "user",
                "content": "Respond in the voice of this character."
            }
        ],
        "max_tokens": 150,
        "temperature": 0.7
    }
    
    # Make the API request
    try:
        response = requests.post(OPENAI_URL, headers=headers, json=payload)
        response.raise_for_status()  # Raise exception for HTTP errors
        
        # Parse the response
        result = response.json()
        
        # Extract the generated text
        generated_text = result['choices'][0]['message']['content']
        return generated_text
        
    except Exception as e:
        print(f"Error calling OpenAI API: {e}")
        return f"Sorry, I couldn't generate a response for {persona}."
    return sys_prompt


if __name__ == '__main__':
    # Run the app on port 5000
    app.run(host='0.0.0.0', port=4444, debug=True)
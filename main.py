import argparse
import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
api_key = os.environ.get("OPENROUTER_API_KEY")

if api_key is None:
    raise RuntimeError("No API Key Found!")

# Client setup
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=api_key,
)

# Parser
parser = argparse.ArgumentParser(description="Chatbot")
parser.add_argument("user_prompt", type=str, help="User prompt")
args = parser.parse_args()

messages = [
    {"role": "user", "content": args.user_prompt},
]

# Response setup
response = client.chat.completions.create(
    model="openrouter/free",
    messages=messages,
)

usage = response.usage
if usage is None:
    raise RuntimeError("Failed API request")

prompt_tokens = response.usage.prompt_tokens
completion_tokens: int = response.usage.completion_tokens
print(f"User prompt: {args.user_prompt}")
print(f"Prompt tokens: {prompt_tokens}")
print(f"Response tokens: {completion_tokens}")
print("Response:")
print(response.choices[0].message.content)

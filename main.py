import argparse
import json
import os

from dotenv import load_dotenv
from openai import OpenAI

from call_function import available_functions, call_function
from prompts import system_prompt

load_dotenv()
api_key = os.environ.get("OPENROUTER_API_KEY")

if api_key is None:
    raise RuntimeError("No API Key Found!")

# Client setup
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=api_key,
)

# Parser and Arguments
parser = argparse.ArgumentParser(description="Chatbot")
parser.add_argument("user_prompt", type=str, help="User prompt")
parser.add_argument("--verbose", action="store_true", help="Enable verbose outputs")
args = parser.parse_args()

messages = [
    {"role": "system", "content": system_prompt},
    {"role": "user", "content": args.user_prompt},
]

# Response setup
response = client.chat.completions.create(
    model="openrouter/free",
    messages=messages,
    tools=available_functions,
    temperature=0,
)

usage = response.usage
if usage is None:
    raise RuntimeError("Failed API request")

message = response.choices[0].message

prompt_tokens = response.usage.prompt_tokens
completion_tokens: int = response.usage.completion_tokens
if args.verbose:
    print(f"User prompt: {args.user_prompt}")
    print(f"Prompt tokens: {prompt_tokens}")
    print(f"Response tokens: {completion_tokens}")
    print("response:")
    print(message.content)
if message.tool_calls:
    for tool_call in message.tool_calls:
        result_message = call_function(tool_call, verbose=args.verbose)

        if not result_message.get("content"):
            raise RuntimeError("Error: Empty function result")

        if args.verbose:
            print(f"-> {result_message['content']}")
else:
    print("response:")
    print(message.content)

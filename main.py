import argparse
import os
import sys

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

# Parser and arguments
parser = argparse.ArgumentParser(description="Chatbot")
parser.add_argument("user_prompt", type=str, help="User prompt")
parser.add_argument("--verbose", action="store_true", help="Enable verbose outputs")
args = parser.parse_args()

messages = [
    {"role": "system", "content": system_prompt},
    {"role": "user", "content": args.user_prompt},
]

for _ in range(20):
    # Request completion
    response = client.chat.completions.create(
        model="openrouter/free",
        messages=messages,
        tools=available_functions,
        temperature=0,
    )

    # Validate API response
    usage = response.usage
    if usage is None:
        raise RuntimeError("Failed API request")

    message = response.choices[0].message
    messages.append(
        message
    )  # Assistant's message: to keep the model aware of the conversation so far

    # Verbose output
    if args.verbose:
        print(f"User prompt: {args.user_prompt}")
        print(f"Prompt tokens: {usage.prompt_tokens}")
        print(f"Response tokens: {usage.completion_tokens}")

    # Handle tool calls or regular response
    if message.tool_calls:
        for tool_call in message.tool_calls:
            result_message = call_function(tool_call, verbose=args.verbose)
            messages.append(result_message)  # Tool message: append the tool calls

            if not result_message.get("content"):
                raise RuntimeError("Error: Empty function result")

            if args.verbose:
                print(f"-> {result_message['content']}")
    else:
        print("response:")
        print(message.content)
        break  # No more tool calls, final response, therefore break

    if _ == 20:
        print("Maximum number of iterations has been reached, task was not finished")
        sys.exit(1)

from openrouter import OpenRouter
import os

from src.env_loader import API_KEY

with OpenRouter(api_key=API_KEY) as client:
    response = client.chat.send(
        model="~openai/gpt-latest",
        messages=[
            {"role": "user", "content": "Write a Ballerina function that takes a list of numbers and returns the sum of the even numbers in the list."}
        ],
    )

    print(response.choices[0].message.content)
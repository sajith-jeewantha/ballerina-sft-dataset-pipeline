import requests

# instead of scraping https://github.com/ballerina-platform/module-ballerina-io/blob/main/README.md
# fetch the raw file directly:
url = "https://github.com/ballerina-platform/module-ballerinax-sap/blob/main/ballerina/constants.bal"
resp = requests.get(url)
content = resp.text  # plain text/markdown, zero HTML, zero JS, zero GitHub chrome

print(content)

# GitHub REST API — clean JSON, no scraping needed
# resp = requests.get("https://api.github.com/repos/ballerina-platform/module-ballerina-io")
# data = resp.json()
# print(data["description"], data["default_branch"])
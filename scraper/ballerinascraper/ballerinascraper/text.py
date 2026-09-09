import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)


print(f"BASE_DIR: {BASE_DIR}")
print(f'PROJECT_ROOT: {PROJECT_ROOT}')

path = os.path.join(PROJECT_ROOT, 'proxy','proxy_list.txt')
print(path)

if 'ballerinascraper/proxy/proxy_list.txt' in path:
    print(True)
else:
    print(False)
# ballerinascraper/proxy/proxy_list.txt
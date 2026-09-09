import requests

url = "https://proxylist.geonode.com/api/proxy-list"
def fetch_all_proxies(pages=5, limit=500):
    all_proxies = []
    for page in range(1, pages + 1):
        resp = requests.get(url, params={"page": page, "limit": limit, "sort_by": "responseTime", "sort_type": "asc"}, timeout=30)
        data = resp.json().get("data", [])
        if not data:
            break
        all_proxies.extend(f"{e['ip']}:{e['port']}" for e in data)
    return all_proxies

proxies = fetch_all_proxies(pages=5)
with open("../proxy/proxy_list.txt", "w") as f:
    f.write("\n".join(proxies))
print(f"Wrote {len(proxies)} proxies")
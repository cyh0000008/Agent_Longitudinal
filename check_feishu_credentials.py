import requests

from feishu_config import get_feishu_credentials


def main():
    app_id, app_secret = get_feishu_credentials()
    response = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={
            "app_id": app_id,
            "app_secret": app_secret,
        },
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()

    if data.get("code") != 0:
        raise RuntimeError(f"Feishu auth failed: {data}")

    token = data["tenant_access_token"]
    print("Feishu credentials OK")
    print("tenant_access_token:", token[:8] + "..." + token[-6:])
    print("expire:", data.get("expire"))


if __name__ == "__main__":
    main()

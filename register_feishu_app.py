import lark_oapi as lark


def handle_qr(info):
    print("打开这个链接授权：", flush=True)
    print(info["url"], flush=True)
    print(f"有效期：{info['expire_in']} 秒", flush=True)


def handle_status(info):
    print("状态：", info["status"], flush=True)


result = lark.register_app(
    on_qr_code=handle_qr,
    on_status_change=handle_status,
    app_preset={
        "name": "布鲁-{user}",
        "desc": "用于飞书消息入口、日志分析、数据总结的智能体",
    },
)

print("App ID:", result.get("client_id") or result.get("app_id"), flush=True)
print("App Secret:", result.get("client_secret") or result.get("app_secret"), flush=True)
print("User Info:", result.get("user_info"), flush=True)

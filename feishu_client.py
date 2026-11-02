import lark_oapi as lark

from feishu_config import get_feishu_credentials


def build_client():
    app_id, app_secret = get_feishu_credentials()
    return (
        lark.Client.builder()
        .app_id(app_id)
        .app_secret(app_secret)
        .build()
    )

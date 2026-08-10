"""mclaw 工具的一级服务目录渲染（docs/design/agent-cli-integration.md §2）。

目录进工具 description，让模型「知道去哪个域找」；二级以下坚决不进
（--help 现查）。数据源是 CLI 内置 spec——同仓构建保证与真实命令面同版；
每域的一行说明在 _DOMAIN_LINES 手工润色（信息密度比 DOMAIN_HELP 的短标签
高），守护测试保证与 spec 的域集合严格同步。
"""

from __future__ import annotations

import logging
from functools import lru_cache

from movieclaw_api.exceptions import AppException
from movieclaw_cli.core.errors import CliError
from movieclaw_cli.gen.spec_loader import load_baseline
from movieclaw_cli.gen.tree_builder import DOMAIN_HELP, is_generable, iter_operations

logger = logging.getLogger("movieclaw_api.mclaw_tool")

# 域 → 目录行（一行说明 + 关键子能力/入口提示）。新增域忘了补会被守护测试拦下，
# 届时可先回落 DOMAIN_HELP 的短标签保证不漏。
_DOMAIN_LINES = {
    "channels": "channels 通知渠道（微信扫码绑定与账号管理）",
    "notices": "notices  系统待处理事项（list 查看活跃问题、dismiss 忽略）",
    "search": 'search   站点资源搜索：search "关键词" 流式聚合出带行号的结果',
    "sub": "sub      订阅：sub create 一步完成消歧/预检/创建；list/show/update/pause/delete",
    "lib": "lib      媒体库：库管理、scan 扫描、organize 整理、items 条目、"
    "unidentified 待识别认领、missing 缺失重下、review 身份复核",
    "site": "site     PT 站点接入与验证（catalog 看支持哪些站）",
    "dl": "dl       下载器接入、默认下载器与路径映射",
    "watch": "watch    监听导入规则（下载完成目录 → 自动入库）",
    "webhook": "webhook  事件 Webhook 出站推送（show/set 配置端点、test 试发、"
    "deliveries 查投递记录、rotate-secret 轮换签名密钥）",
    "rules": "rules    订阅过滤规则组（分辨率/制作组/体积等偏好）",
    "discover": "discover 影视元数据与榜单（media 搜索条目、拿 TMDB ID）",
    "people": "people   影人档案（本地库）",
    "llm": "llm      AI 模型供应商配置",
    "ui": "ui       界面偏好（Web 端玻璃质感参数）",
    "net": "net      网络与代理（show/set/test）",
    "auth": "auth     账号、API 令牌管理",
    "appearance": "appearance 外观背景图库",
    "extension": "extension 浏览器插件 Cookie 同步令牌",
    "health": "health   服务健康检查（一般用 status 即可）",
}

# 不属于生成域、但必须让模型知道的顶级捷径
_TOP_LEVEL_LINES = [
    "download 下载：download <行号> 提交上次 search 结果的某行（或 --site-id + --url）",
    "status   一眼看部署状态：服务健康、登录身份、版本同步",
]

# 不进目录的域：
# - agent：被工具硬闸禁止（递归），目录里出现只会误导模型；
# - logs：对 Agent 是 bash 的弱化重复——日志就是同容器内的本地文件（路径已写进
#   系统提示词环境段），grep/tail 能力更强；且 logs tail -f 永不退出，模型误用
#   会干等到工具超时。CLI 命令保留，服务远程管理的人类用户。
# - members：成员管理是高敏感操作（建号、重置密码、启停、改权限），属于
#   部署者本人的账号治理，绝不该由对话式 Agent 代劳（Agent 令牌是超管级，
#   放进目录等于把开号/改权限的能力交给模型）。CLI 命令保留给人类管理员。
_EXCLUDED_DOMAINS = {"agent", "logs", "members"}


def spec_domains() -> set[str]:
    """spec 里全部会生成命令、且对模型开放的域（守护测试与渲染共用）。"""
    return {
        op["operation_id"].split(".")[0]
        for op in iter_operations(load_baseline())
        if is_generable(op)
    } - _EXCLUDED_DOMAINS


@lru_cache(maxsize=1)
def render_service_map() -> str:
    """渲染一级服务目录（进程内缓存；spec 是构建期产物，运行期不变）。

    spec 装载失败只可能是部署产物不完整（基线文件没进镜像/安装包）。这类
    故障必须给出可读结论：CliError 是 CLI 侧的异常，逃到 API 层会被兜底
    处理器变成一句裸的 "internal server error"，自部署用户根本无从判断该
    重新部署还是该改配置。
    """
    try:
        domains = sorted(spec_domains())
    except CliError as exc:
        logger.error("CLI 基线 spec 装载失败，Agent 无法组装 mclaw 工具：%s", exc)
        raise AppException(
            status_code=500,
            code="SPEC_BASELINE_MISSING",
            message=(
                "服务端缺少 CLI 基线 spec（src/movieclaw_cli/data/spec.json），"
                "Agent 功能不可用。这通常是镜像或安装包不完整，请更新到新版镜像后重新部署。"
            ),
        ) from exc

    lines = ['可用服务（一级目录；参数细节用 --help 现查，如 args="sub --help"）：']
    for domain in domains:
        lines.append("- " + _DOMAIN_LINES.get(domain, f"{domain}   {DOMAIN_HELP.get(domain, '')}"))
    for extra in _TOP_LEVEL_LINES:
        lines.append("- " + extra)
    return "\n".join(lines)

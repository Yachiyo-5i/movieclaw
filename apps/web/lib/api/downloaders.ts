import { request } from "@/lib/http";

/** 后端统一响应信封（见 movieclaw_api.schemas.response.ApiResponse） */
interface ApiEnvelope<T> {
  success: boolean;
  code: string;
  message: string;
  data: T;
}

async function unwrap<T>(promise: Promise<ApiEnvelope<T>>): Promise<T> {
  return (await promise).data;
}

/** 已适配的下载器类型（与后端 movieclaw_db ClientType 对应）。 */
export type DownloaderClientType = "qbittorrent" | "transmission";

/** 连接验证状态（与站点配置共用同一状态机语义）。 */
export type DownloaderStatus = "pending" | "verifying" | "active" | "failed";

/**
 * 一条路径映射：movieclaw 视角的目录前缀 → 下载器视角的对应前缀。
 * 跨容器/跨主机部署同一块盘两边路径不同时配置；视角一致留空。
 */
export interface PathMapping {
  /** movieclaw 上的路径前缀（目录弹窗选择） */
  local: string;
  /** 下载器上的对应路径前缀（手动填写） */
  remote: string;
}

/** 已配置下载器的对外视图（见 schemas.downloader.DownloaderView，脱敏无密码）。 */
export interface ConfiguredDownloader {
  id: number;
  name: string;
  client_type: DownloaderClientType;
  url: string;
  username: string | null;
  save_path: string | null;
  /** 路径映射（movieclaw 路径 → 下载器路径）；null = 两边视角一致 */
  path_mappings: PathMapping[] | null;
  enabled: boolean;
  /** 是否为默认下载器（一键下载不选目标时投给它） */
  is_default: boolean;
  status: DownloaderStatus;
  /** 是否可用 = 已启用且连接测试通过 */
  usable: boolean;
  /** 最近一次连接成功获取的版本号，如 "v5.0.2" */
  version: string | null;
  last_error: string | null;
  last_checked_at: string | null;
  created_at: string;
  updated_at: string;
}

export type DownloadTaskState =
  | "downloading"
  | "stalled"
  | "paused"
  | "completed"
  | "error"
  | "missing"
  | "unknown";

export interface DownloadTaskSubscription {
  id: number;
  media_item_id: number;
  media_title: string;
  media_kind: string;
  poster_url: string | null;
  units: { season_number: number; episode_number: number }[];
}

/** 下载器实时任务；订阅/手动来源由后端按 infohash 关联，不复制下载状态。 */
export interface DownloadTask {
  id: string;
  info_hash: string;
  name: string | null;
  downloader_id: number | null;
  downloader_name: string | null;
  downloader_type: DownloaderClientType | null;
  progress: number | null;
  size_bytes: number | null;
  dlspeed_bytes: number | null;
  eta_seconds: number | null;
  state: DownloadTaskState;
  source: "subscription" | "manual" | "external";
  media_item_id: number | null;
  media_title: string | null;
  media_kind: string | null;
  poster_url: string | null;
  subscriptions: DownloadTaskSubscription[];
}

export interface DownloadTaskSource {
  id: number;
  name: string;
  client_type: DownloaderClientType;
  status: "active" | "disabled" | "unavailable" | "error";
  message: string | null;
  task_count: number;
}

export interface DownloadTaskSnapshot {
  items: DownloadTask[];
  sources: DownloadTaskSource[];
}

/** 新增/更新下载器的请求体（见 schemas.downloader.DownloaderPayload）。 */
export interface DownloaderPayload {
  name: string;
  client_type: DownloaderClientType;
  url: string;
  username?: string | null;
  password?: string | null;
  save_path?: string | null;
  path_mappings?: PathMapping[] | null;
  enabled?: boolean;
}

/** 列出所有已配置的下载器及连接状态。 */
export function listDownloaders(init?: RequestInit): Promise<ConfiguredDownloader[]> {
  return unwrap(request<ApiEnvelope<ConfiguredDownloader[]>>("/downloaders", init));
}

/** 任务中心快照：所有下载器的活跃任务、仍待入库任务与来源健康状态。 */
export function listDownloadTasks(init?: RequestInit): Promise<DownloadTaskSnapshot> {
  return unwrap(request<ApiEnvelope<DownloadTaskSnapshot>>("/downloaders/tasks", init));
}

/** 获取单个下载器详情（用于轮询连接测试进度）。 */
export function getDownloader(id: number, init?: RequestInit): Promise<ConfiguredDownloader> {
  return unwrap(request<ApiEnvelope<ConfiguredDownloader>>(`/downloaders/${id}`, init));
}

/** 添加一个下载器（保存后后端异步测试连接）。 */
export function createDownloader(payload: DownloaderPayload): Promise<ConfiguredDownloader> {
  return unwrap(
    request<ApiEnvelope<ConfiguredDownloader>>("/downloaders", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  );
}

/** 更新下载器配置（更新后后端重新测试连接）。 */
export function updateDownloader(
  id: number,
  payload: DownloaderPayload,
): Promise<ConfiguredDownloader> {
  return unwrap(
    request<ApiEnvelope<ConfiguredDownloader>>(`/downloaders/${id}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  );
}

/** 启用 / 停用下载器。 */
export function setDownloaderEnabled(
  id: number,
  enabled: boolean,
): Promise<ConfiguredDownloader> {
  return unwrap(
    request<ApiEnvelope<ConfiguredDownloader>>(`/downloaders/${id}/status`, {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    }),
  );
}

/** 设为默认下载器。注意：其他条目的 is_default 会随之变化，调用后应整体刷新列表。 */
export function setDefaultDownloader(id: number): Promise<ConfiguredDownloader> {
  return unwrap(
    request<ApiEnvelope<ConfiguredDownloader>>(`/downloaders/${id}/default`, { method: "POST" }),
  );
}

/** 手动重新测试一次连接。 */
export function reverifyDownloader(id: number): Promise<ConfiguredDownloader> {
  return unwrap(
    request<ApiEnvelope<ConfiguredDownloader>>(`/downloaders/${id}/verify`, { method: "POST" }),
  );
}

/** 删除下载器配置。 */
export function deleteDownloader(id: number): Promise<Record<string, never>> {
  return unwrap(
    request<ApiEnvelope<Record<string, never>>>(`/downloaders/${id}`, { method: "DELETE" }),
  );
}

/** 手动提交下载的请求体（见 schemas.downloader.DownloadSubmitPayload）。 */
export interface DownloadSubmitPayload {
  /** 种子所属站点 ID（TorrentHit.site_id） */
  site_id: string;
  /** 种子下载入口（TorrentHit.download_url） */
  download_url: string;
  /** 入库目标库（可选）：带上后保存目录由库推导（主根/标题 (年份)） */
  library_id?: number | null;
  /** 条目标题（推导条目子目录用；身份未确认时不要带） */
  title?: string | null;
  year?: number | null;
  /** 种子副标题（识别线索：中文片名/「全N集」帮扫描器收敛拼音命名种子） */
  subtitle?: string | null;
  /** 下载弹窗手选的保存目录（movieclaw 视角，优先于库推导） */
  save_path?: string | null;
  /** 指定投递到哪台下载器（配了多台按需分流）；缺省用默认下载器 */
  downloader_id?: number | null;
  /** 服务端按已确认的 TMDB 身份重新匹配媒体库并选择监听导入目录 */
  auto_route?: boolean;
  /** 智能入库的媒体类型，与 tmdb_id 一起构成后端路由输入 */
  media_kind?: "movie" | "tv";
  /** 智能入库已确认的 TMDB 条目 ID */
  tmdb_id?: number;
}

/** 手动下载识别未收敛时返回的 TMDB 候选（供界面解释为何不自动投递）。 */
export interface ManualDownloadTargetCandidate {
  tmdb_id: number;
  title: string;
  year: number | null;
  episode_count: number | null;
}

/** 手动搜索种子的「识别 → 库路由 → 监听投递目录」预检结果。 */
export interface ManualDownloadTarget {
  status: "ready" | "ambiguous" | "not_found";
  tmdb_id: number | null;
  candidates: ManualDownloadTargetCandidate[];
  library_id: number | null;
  library_name: string | null;
  mode: "watch" | "inplace" | "downloader_default" | null;
  path: string | null;
  staging_path: string | null;
  route_matched: boolean | null;
  route_reason: string | null;
  ok: boolean;
  warning: string | null;
}

/** 预演一条搜索结果能否被可靠识别并投递到匹配库的监听目录。 */
export function resolveManualDownloadTarget(payload: {
  kind: "movie" | "tv";
  title: string;
  year: number;
  subtitle?: string | null;
  /** 用这台下载器的路径映射预检；缺省沿用后端默认下载器语义 */
  downloader_id?: number | null;
  /** 歧义时由用户确认的、且必须属于本次候选的 TMDB ID */
  selected_tmdb_id?: number | null;
}): Promise<ManualDownloadTarget> {
  return unwrap(
    request<ApiEnvelope<ManualDownloadTarget>>("/downloaders/resolve-target", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  );
}

/** 手动提交下载的结果（见 schemas.downloader.DownloadSubmitView）。 */
export interface DownloadSubmitResult {
  info_hash: string | null;
  name: string;
  /** 种子提交前已存在于下载器（幂等，未重复添加） */
  already_exists: boolean;
  downloader_id: number;
  downloader_name: string;
  /** 实际使用的保存目录（null = 下载器自身默认目录） */
  save_path: string | null;
}

/**
 * 把一条搜索结果种子提交到默认下载器：后端带站点登录态取回 .torrent 再递交，
 * 保存目录用默认下载器配置的默认目录。失败抛 HttpError，message 为可读中文。
 */
export function submitTorrentDownload(
  payload: DownloadSubmitPayload,
): Promise<DownloadSubmitResult> {
  return unwrap(
    request<ApiEnvelope<DownloadSubmitResult>>("/downloaders/submit", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  );
}

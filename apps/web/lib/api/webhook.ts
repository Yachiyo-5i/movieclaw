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

export type WebhookFormat = "movieclaw" | "jellyfin";
export type EgressScope = "lan" | "wan";

/** 一条投递记录（内存环形缓冲，重启清空；见 schemas.webhook.DeliveryView）。 */
export interface WebhookDelivery {
  event_id: string;
  event: string;
  ok: boolean;
  status_code: number | null;
  duration_ms: number;
  attempts: number;
  error: string;
  at: string;
}

/** 事件目录条目：前端按 group 分组渲染订阅多选。 */
export interface WebhookCatalogEntry {
  event: string;
  group: string;
  label: string;
  /** false = 该事件没有 Jellyfin 对应物，jellyfin 格式 endpoint 不可订阅 */
  jellyfin_supported: boolean;
  /** false = 高频事件（如 playback.progress），新建 endpoint 时默认不勾选 */
  default_on: boolean;
}

export interface WebhookEndpointView {
  id: string;
  name: string;
  url: string;
  format: WebhookFormat;
  enabled: boolean;
  events: string[];
  template: string;
  headers: Record<string, string>;
  egress_scope: EgressScope;
  secret_masked: string;
  /** 明文——仅在新建/轮换的那一次响应里出现，前端须立刻展示并提示保存 */
  secret: string | null;
  last_delivery: WebhookDelivery | null;
}

export interface WebhookConfigView {
  enabled: boolean;
  endpoints: WebhookEndpointView[];
  catalog: WebhookCatalogEntry[];
}

/** 保存请求体里的单个 endpoint。id 为空 = 新建（服务端生成 id 与 secret）。 */
export interface WebhookEndpointPayload {
  id: string;
  name: string;
  url: string;
  format: WebhookFormat;
  enabled: boolean;
  events: string[];
  template: string;
  headers: Record<string, string>;
  egress_scope: EgressScope;
}

export interface WebhookConfigPayload {
  enabled: boolean;
  endpoints: WebhookEndpointPayload[];
}

export function getWebhookConfig(): Promise<WebhookConfigView> {
  return unwrap(request<ApiEnvelope<WebhookConfigView>>("/webhook"));
}

export function saveWebhookConfig(payload: WebhookConfigPayload): Promise<WebhookConfigView> {
  return unwrap(
    request<ApiEnvelope<WebhookConfigView>>("/webhook", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  );
}

/** 轮换签名密钥；返回的 endpoint 带一次性明文 secret。 */
export function rotateWebhookSecret(endpointId: string): Promise<WebhookEndpointView> {
  return unwrap(
    request<ApiEnvelope<WebhookEndpointView>>(
      `/webhook/endpoints/${encodeURIComponent(endpointId)}/rotate-secret`,
      { method: "POST" },
    ),
  );
}

/** 发送示例事件，同步返回投递结果。 */
export function sendWebhookTest(endpointId: string): Promise<WebhookDelivery> {
  return unwrap(
    request<ApiEnvelope<WebhookDelivery>>(
      `/webhook/endpoints/${encodeURIComponent(endpointId)}/test`,
      { method: "POST" },
    ),
  );
}

export function listWebhookDeliveries(endpointId: string): Promise<WebhookDelivery[]> {
  return unwrap(
    request<ApiEnvelope<WebhookDelivery[]>>(
      `/webhook/endpoints/${encodeURIComponent(endpointId)}/deliveries`,
    ),
  );
}

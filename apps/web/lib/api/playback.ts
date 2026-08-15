import { request } from "@/lib/http";
import type { MediaType } from "@/lib/media-types";

interface ApiEnvelope<T> {
  success: boolean;
  code: string;
  message: string;
  data: T;
}

export interface RecentWatchItem {
  media_item_id: number;
  library_id: number;
  kind: MediaType;
  title: string;
  year: number | null;
  poster_url: string | null;
  /** 电影横向背景剧照；缺失时前端用竖版海报生成模糊铺底。 */
  backdrop_url: string | null;
  /** 剧集最近播放那一集的 16:9 剧照；电影恒为 null。 */
  episode_still_url: string | null;
  season_number: number;
  episode_number: number;
  episode_title: string | null;
  /** 最近一次播放之后，同一媒体库中新进入且仍在位的分集数；电影恒为 0。 */
  new_episode_count: number;
  position_ms: number;
  duration_ms: number | null;
  progress_percent: number | null;
  played: boolean;
  play_count: number;
  last_played_at: string;
}

/** 当前账号在可见媒体库中的最近观看作品。 */
export async function listRecentWatch(limit = 20): Promise<RecentWatchItem[]> {
  const response = await request<ApiEnvelope<{ items: RecentWatchItem[] }>>(
    `/playback/recent?limit=${limit}`,
  );
  return response.data.items;
}

"use client";

import type { Route } from "next";
import Link from "next/link";

import { HScroller } from "@/components/h-scroller";
import { CheckIcon } from "@/components/icons";
import { PosterImage } from "@/components/poster-image";
import type { RecentWatchItem } from "@/lib/api/playback";
import { imageUrl } from "@/lib/image-proxy";
import { formatRelativeTime } from "@/lib/time";
import { useTapGuard } from "@/lib/use-tap-guard";

/** S1E2 统一展示为播放器常见的 S01E02，扫一眼即可辨认具体集。 */
function episodeCode(item: RecentWatchItem): string {
  return `S${String(item.season_number).padStart(2, "0")}E${String(item.episode_number).padStart(2, "0")}`;
}

function itemHref(item: RecentWatchItem): Route {
  const base = `/library/${item.library_id}/item/${item.media_item_id}`;
  if (item.kind === "tv") {
    return `${base}?season=${item.season_number}&episode=${item.episode_number}&from=recent` as Route;
  }
  return `${base}?from=recent` as Route;
}

function stateLabel(item: RecentWatchItem): string {
  const ago = formatRelativeTime(item.last_played_at);
  if (item.played) return `${ago}已看完`;
  if (item.position_ms > 0 && item.progress_percent != null) {
    return `${ago}观看到 ${item.progress_percent}%`;
  }
  if (item.position_ms > 0) return `${ago}观看过`;
  return `${ago}播放过`;
}

/** 媒体库首页顶部的最近观看横排；空列表整段隐藏。 */
export function RecentWatchRow({ items }: { items: RecentWatchItem[] | null }) {
  // 首页不存在观看记录时完全不占位；首次请求尚未返回也先保持原布局，
  // 避免从未使用播放器的用户看到一个没有实际内容的分区骨架。
  if (!items?.length) return null;

  return (
    <section className="mt-8 max-md:mt-6" aria-labelledby="recent-watch-title">
      <div className="px-6 max-md:px-4">
        <h3
          id="recent-watch-title"
          className="text-on-image text-body-lg font-semibold tracking-[-0.01em] text-[var(--text)]"
        >
          最近观看
        </h3>
      </div>
      <HScroller className="mt-3 gap-4 px-6 pb-2 pt-1 max-md:gap-3 max-md:px-4">
        {items.map((item) => <RecentWatchCard key={item.media_item_id} item={item} />)}
      </HScroller>
    </section>
  );
}

function RecentWatchCard({ item }: { item: RecentWatchItem }) {
  const tapGuard = useTapGuard();
  const code = item.kind === "tv" ? episodeCode(item) : null;
  const context =
    item.kind === "tv"
      ? item.episode_title
      : item.year
        ? String(item.year)
        : "";
  const progress = item.played ? 100 : item.progress_percent;
  const isEpisode = item.kind === "tv";
  const artworkUrl = isEpisode ? item.episode_still_url : item.backdrop_url;
  const newEpisodeLabel =
    isEpisode && item.new_episode_count > 0 ? `新入库 ${item.new_episode_count} 集` : null;

  return (
    <Link
      href={itemHref(item)}
      aria-label={`${item.title}${context ? `，${context}` : ""}，${stateLabel(item)}${newEpisodeLabel ? `，${newEpisodeLabel}` : ""}`}
      {...tapGuard}
      className="group/recent w-[224px] shrink-0 outline-none max-md:w-[200px] xl:w-[240px]"
    >
      <div
        className="relative aspect-video overflow-hidden rounded-2xl bg-[#141824] shadow-[0_10px_28px_rgba(0,0,0,0.38)] ring-1 ring-white/[0.08] transition duration-300 group-hover/recent:-translate-y-1 group-hover/recent:shadow-[0_18px_42px_rgba(0,0,0,0.55)] group-hover/recent:ring-white/25 group-focus-visible/recent:ring-2 group-focus-visible/recent:ring-white/80"
      >
        {artworkUrl ? (
          <PosterImage
            src={imageUrl(artworkUrl, "landscape-card")}
            alt={isEpisode ? `${item.title} ${code}分集剧照` : `${item.title}背景剧照`}
            className="size-full transition duration-500 group-hover/recent:scale-[1.03]"
            fallback={
              isEpisode ? (
                <span className="tnum flex size-full items-center justify-center text-title-sm font-bold tracking-wide text-white/25">
                  {code}
                </span>
              ) : (
                <MoviePosterFill title={item.title} posterUrl={item.poster_url} />
              )
            }
          />
        ) : isEpisode ? (
          <span className="tnum flex size-full items-center justify-center text-title-sm font-bold tracking-wide text-white/25">
            {code}
          </span>
        ) : (
          <MoviePosterFill title={item.title} posterUrl={item.poster_url} />
        )}
        <div className="pointer-events-none absolute inset-x-0 bottom-0 h-16 bg-gradient-to-t from-black/75 to-transparent" />
        {code && (
          <span className="tnum absolute left-2 top-2 rounded-md border border-white/15 bg-black/65 px-1.5 py-0.5 text-micro font-semibold tracking-wide text-white backdrop-blur-md">
            {code}
          </span>
        )}
        {newEpisodeLabel && (
          <span className="tnum absolute right-2 top-2 rounded-full border border-emerald-200/25 bg-[rgba(5,46,34,0.76)] px-2 py-0.5 text-micro font-semibold text-emerald-100 shadow-[0_5px_16px_rgba(0,0,0,0.32)] backdrop-blur-md">
            {newEpisodeLabel}
          </span>
        )}
        {item.played && (
          <span className="absolute bottom-2 right-2 flex size-6 items-center justify-center rounded-full bg-emerald-400 text-[#07120c] shadow-lg">
            <CheckIcon className="size-3.5 stroke-[2.5]" />
          </span>
        )}
        {progress != null && (
          <div className="absolute inset-x-2 bottom-2 h-[3px] overflow-hidden rounded-full bg-white/25">
            <div
              className={`h-full rounded-full ${item.played ? "bg-emerald-400" : "bg-[var(--accent-2)]"}`}
              style={{ width: `${progress}%` }}
            />
          </div>
        )}
        {!item.played && item.position_ms > 0 && progress == null && (
          <div className="absolute inset-x-2 bottom-2 h-[3px] rounded-full bg-[var(--accent-2)]/60" />
        )}
      </div>
      <p className="mt-2 truncate text-ui font-semibold text-[var(--text)]">{item.title}</p>
      {context && (
        <p className="tnum mt-0.5 truncate text-sub text-[var(--text-muted)]">{context}</p>
      )}
      <p className="tnum mt-0.5 truncate text-caption text-[var(--text-faint)]">
        {stateLabel(item)}
      </p>
    </Link>
  );
}

/** 电影缺少横向剧照时：海报模糊铺底，中央按 2:3 完整保留一张清晰海报。 */
function MoviePosterFill({ title, posterUrl }: { title: string; posterUrl: string | null }) {
  if (!posterUrl) {
    return (
      <span className="flex size-full items-center justify-center px-5 text-center text-ui font-semibold text-white/25">
        {title}
      </span>
    );
  }
  const src = imageUrl(posterUrl, "poster-card");
  return (
    <div className="relative size-full overflow-hidden bg-[#10131c]">
      <PosterImage
        src={src}
        alt=""
        className="absolute inset-0 size-full scale-125 blur-xl opacity-45"
      />
      <div className="absolute inset-0 bg-black/25" />
      <div className="absolute inset-y-0 left-1/2 w-[37.5%] -translate-x-1/2 shadow-[0_0_28px_rgba(0,0,0,0.55)]">
        <PosterImage src={src} alt={`${title}海报`} className="size-full" />
      </div>
    </div>
  );
}

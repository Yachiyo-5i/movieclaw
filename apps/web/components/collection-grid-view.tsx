"use client";

import { useDeferredValue, useEffect, useMemo, useRef, useState } from "react";

import { PageNav } from "@/components/page-nav";
import { SearchIcon } from "@/components/icons";
import { PosterCard } from "@/components/poster-card";
import { browseDiscoveryCollection } from "@/lib/api/discover";
import type { MediaItem } from "@/lib/media-types";
import { useScrollRestoration } from "@/lib/use-scroll-restoration";

const PAGE_SIZE = 50;
const MAX_COLLECTION_VISIBLE_COUNTS = 32;
const collectionVisibleCounts = new Map<string, number>();

function getCollectionVisibleCount(collectionRef: string) {
  return collectionVisibleCounts.get(collectionRef) ?? PAGE_SIZE;
}

function rememberCollectionVisibleCount(collectionRef: string, count: number) {
  collectionVisibleCounts.delete(collectionRef);
  collectionVisibleCounts.set(collectionRef, count);
  while (collectionVisibleCounts.size > MAX_COLLECTION_VISIBLE_COUNTS) {
    const oldest = collectionVisibleCounts.keys().next().value;
    if (oldest === undefined) break;
    collectionVisibleCounts.delete(oldest);
  }
}

/**
 * 完整片单落地页（「看全部」的目的地）：用纵向网格承载大量条目。
 * collectionRef 同时携带来源、媒体类型和片单身份，页面不再为豆瓣榜单
 * 硬编码专用接口；前端只分批挂载图片节点，控制首屏开销。
 */
export function CollectionGridView({
  collectionRef,
}: {
  /** 由发现页展示清单返回的稳定片单引用。 */
  collectionRef: string;
}) {
  const scrollRef = useScrollRestoration(`collection:${collectionRef}`);
  const [items, setItems] = useState<MediaItem[] | null>(null);
  const [title, setTitle] = useState("影视片单");
  const [query, setQuery] = useState("");
  const [selectedGenres, setSelectedGenres] = useState<string[]>([]);
  const [visibleCount, setVisibleCount] = useState(() => getCollectionVisibleCount(collectionRef));
  const [error, setError] = useState<string | null>(null);
  const loadMoreRef = useRef<HTMLDivElement>(null);
  const previousCollectionRef = useRef(collectionRef);
  const [provider, mediaType] = collectionRef.split(":");

  useEffect(() => {
    // 同一组件实例切换榜单时，先切换到新 key 的窗口；不要把旧榜单的数量
    // 在这一轮 effect 中写进新 key。
    if (previousCollectionRef.current !== collectionRef) {
      previousCollectionRef.current = collectionRef;
      setVisibleCount(getCollectionVisibleCount(collectionRef));
      setQuery("");
      setSelectedGenres([]);
      return;
    }
    rememberCollectionVisibleCount(collectionRef, visibleCount);
  }, [collectionRef, visibleCount]);

  useEffect(() => {
    const controller = new AbortController();
    // 同一动态路由切换片单时组件可能被 React 复用，先清掉上一片单的展示态，
    // 避免新请求完成前短暂显示旧榜单，或失败后把旧数据误当成新结果。
    setItems(null);
    setTitle("影视片单");
    setError(null);
    browseDiscoveryCollection(collectionRef, 500, { signal: controller.signal })
      .then((collection) => {
        if (controller.signal.aborted) return;
        setItems(collection.items);
        setTitle(collection.name);
      })
      .catch((reason: Error) => {
        if (!controller.signal.aborted) setError(reason.message || "榜单加载失败，请稍后重试");
      });
    return () => controller.abort();
  }, [collectionRef]);

  const genres = useMemo(() => {
    if (!items) return [];
    const counts = new Map<string, number>();
    for (const item of items) {
      for (const name of item.genres) counts.set(name, (counts.get(name) ?? 0) + 1);
    }
    return [...counts].sort((a, b) => b[1] - a[1]);
  }, [items]);

  // 排名查表：渲染时用 items.indexOf 是每格 O(n) 的线性扫描，数百格一轮渲染
  // 就是数万次比较；榜单加载后排名固定，建一次 Map 终身使用
  const rankById = useMemo(
    () => new Map((items ?? []).map((item, index) => [item.id, index + 1])),
    [items],
  );

  // 搜索输入用延迟值参与过滤：连续敲字时先渲染输入框本身，网格的全量
  // 过滤与重渲染放到浏览器空闲时批量跟上，输入不再一字一卡
  const deferredQuery = useDeferredValue(query);
  const filtered = useMemo(() => {
    const keyword = deferredQuery.trim().toLocaleLowerCase();
    if (!items) return [];
    return items.filter((item) => {
      // 同一筛选维度采用「或」逻辑：选择科幻 + 动画即显示任一类型命中的影片。
      const matchesGenre =
        selectedGenres.length === 0 ||
        selectedGenres.some((selected) => item.genres.includes(selected));
      const matchesKeyword =
        !keyword ||
        item.title.toLocaleLowerCase().includes(keyword) ||
        item.originalTitle.toLocaleLowerCase().includes(keyword);
      return matchesGenre && matchesKeyword;
    });
  }, [items, deferredQuery, selectedGenres]);

  const hasMore = visibleCount < filtered.length;

  // 接近列表底部时自动追加一批；依赖筛选后的总数，切换类型后按新结果重新观察。
  useEffect(() => {
    const target = loadMoreRef.current;
    if (!target || !hasMore) return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setVisibleCount((count) => Math.min(count + PAGE_SIZE, filtered.length));
        }
      },
      { rootMargin: "500px 0px" },
    );
    observer.observe(target);
    return () => observer.disconnect();
  }, [filtered.length, hasMore]);

  const updateQuery = (value: string) => {
    setQuery(value);
    setVisibleCount(PAGE_SIZE);
  };

  const toggleGenre = (value: string) => {
    setSelectedGenres((current) =>
      current.includes(value)
        ? current.filter((selected) => selected !== value)
        : [...current, value],
    );
    setVisibleCount(PAGE_SIZE);
  };

  const clearGenres = () => {
    setSelectedGenres([]);
    setVisibleCount(PAGE_SIZE);
  };

  return (
    <div ref={scrollRef} className="scroll-thin scroll-safe flex-1 overflow-y-auto px-6 pb-12 max-md:px-4">
      {/* 顶栏：返回发现电影（保留豆瓣数据源视角）+ 吸顶榜单名；
          容器已有 px-6，用 -mx-6 让吸顶蒙版铺满整宽 */}
      <PageNav
        items={[
          {
            label: mediaType === "tv" ? "发现剧集" : "发现电影",
            href: `/discover/${mediaType === "tv" ? "tv" : "movie"}?source=${provider === "douban" ? "douban" : "tmdb"}`,
          },
          { label: title },
        ]}
        className="-mx-6 max-md:-mx-4"
      />
      <header className="mx-auto max-w-[1500px]">
        <div className="mt-1 flex flex-col justify-between gap-5 sm:flex-row sm:items-end">
          <div>
            <p className="text-sub font-semibold tracking-[0.18em] text-[var(--accent-2)]">
              {provider.toLocaleUpperCase()} COLLECTION
            </p>
            <h1 className="mt-1 text-3xl font-bold tracking-[-0.03em] text-[var(--text)]">
              {title}
            </h1>
            <p className="mt-2 text-body text-[var(--text-muted)]">
              {items ? `完整收录 ${items.length} 部影片` : "正在读取完整榜单…"}
            </p>
          </div>
          <label className="flex h-10 w-full items-center gap-2 rounded-full border border-white/10 bg-black/25 px-4 text-[var(--text-muted)] backdrop-blur-sm sm:w-72">
            <SearchIcon className="size-4 shrink-0" />
            <input
              value={query}
              onChange={(event) => updateQuery(event.target.value)}
              placeholder="搜索片名"
              aria-label="搜索榜单片名"
              className="min-w-0 flex-1 bg-transparent text-body text-[var(--text)] outline-none placeholder:text-[var(--text-muted)]"
            />
          </label>
        </div>
        {genres.length > 0 && (
          <div className="mt-6 flex flex-wrap items-center gap-2 max-md:mt-4">
            <span className="mr-1 text-sub font-semibold text-[var(--text-muted)]">类型</span>
            <button
              type="button"
              aria-pressed={selectedGenres.length === 0}
              onClick={clearGenres}
              className={`rounded-full border px-3 py-1.5 text-sub font-semibold transition ${
                selectedGenres.length === 0
                  ? "border-white/20 bg-white/15 text-white"
                  : "border-white/[0.07] bg-black/20 text-[var(--text-muted)] hover:border-white/15 hover:text-white"
              }`}
            >
              全部
            </button>
            {genres.map(([name, count]) => (
              <button
                key={name}
                type="button"
                aria-pressed={selectedGenres.includes(name)}
                onClick={() => toggleGenre(name)}
                className={`rounded-full border px-3 py-1.5 text-sub font-semibold transition ${
                  selectedGenres.includes(name)
                    ? "border-white/20 bg-white/15 text-white"
                    : "border-white/[0.07] bg-black/20 text-[var(--text-muted)] hover:border-white/15 hover:text-white"
                }`}
              >
                {name}
                <span className="tnum ml-1 text-micro opacity-55">{count}</span>
              </button>
            ))}
            {selectedGenres.length > 0 && (
              <button
                type="button"
                onClick={clearGenres}
                className="ml-1 rounded-full px-2 py-1.5 text-sub font-semibold text-[var(--accent-2)] transition hover:text-white"
              >
                清除筛选（{selectedGenres.length}）
              </button>
            )}
          </div>
        )}
      </header>

      {error && (
        <div className="mx-auto mt-16 max-w-md rounded-2xl border border-white/10 bg-black/25 p-8 text-center text-body text-[var(--text-muted)]">
          {error}
        </div>
      )}

      {!items && !error && <CollectionSkeleton />}

      {items && (
        <main className="mx-auto mt-8 max-w-[1500px]">
          {filtered.length === 0 ? (
            <div className="py-20 text-center text-body text-[var(--text-muted)]">
              没有找到匹配的影片
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-x-4 gap-y-7 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 2xl:grid-cols-8">
              {filtered.slice(0, visibleCount).map((item) => {
                const rank = rankById.get(item.id) ?? 0;
                return (
                  <div key={item.id} className="relative min-w-0">
                    <RankBadge rank={rank} />
                    <PosterCard item={item} />
                  </div>
                );
              })}
            </div>
          )}

          <div
            ref={loadMoreRef}
            className="mt-8 flex h-10 items-center justify-center text-sub text-[var(--text-muted)]"
            aria-live="polite"
          >
            {hasMore
              ? `继续滚动加载（已显示 ${visibleCount} / ${filtered.length}）`
              : filtered.length > 0
                ? `已显示全部 ${filtered.length} 部影片`
                : ""}
          </div>
        </main>
      )}
    </div>
  );
}

function RankBadge({ rank }: { rank: number }) {
  const tone =
    rank === 1
      ? "bg-[#d8ad50] text-[#211704]"
      : rank === 2
        ? "bg-[#b9c1cc] text-[#171a20]"
        : rank === 3
          ? "bg-[#b9794c] text-[#211108]"
          : "bg-black/70 text-white";
  return (
    <span
      className={`tnum absolute -left-1.5 -top-1.5 z-10 min-w-8 rounded-lg px-2 py-1 text-center text-sub font-black shadow-lg ring-1 ring-white/15 ${tone}`}
    >
      {rank}
    </span>
  );
}

function CollectionSkeleton() {
  return (
    <div className="mx-auto mt-8 grid max-w-[1500px] grid-cols-2 gap-x-4 gap-y-7 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 2xl:grid-cols-8">
      {Array.from({ length: 24 }, (_, index) => (
        <div
          key={index}
          className="aspect-[2/3] animate-pulse rounded-2xl bg-white/[0.05] ring-1 ring-white/10"
        />
      ))}
    </div>
  );
}

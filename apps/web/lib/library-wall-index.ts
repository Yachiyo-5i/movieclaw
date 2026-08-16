interface WallIndexEntry {
  initial: string;
  offset: number;
}

interface WallInitialMarker {
  initial: string;
  top: number;
}

/** 整份拼音排序中的绝对位置 → 所属字母档。 */
export function wallInitialAtOffset(index: readonly WallIndexEntry[], offset: number): string | null {
  return index.findLast((entry) => entry.offset <= offset)?.initial ?? null;
}

/**
 * 字母首部影片的视口位置 → 当前活动字母。
 *
 * 锚点按海报 DOM 顺序传入；最后一个越过滚动容器顶边的锚点就是当前档。
 * 尚未滚到首个锚点时保留分页窗口所属档，避免索引在库头部区域突然无选中。
 */
export function activeWallInitialAtViewport(
  markers: readonly WallInitialMarker[],
  viewportTop: number,
  fallback: string | null,
): string | null {
  let active = fallback;
  for (const marker of markers) {
    if (marker.top > viewportTop) break;
    active = marker.initial;
  }
  return active;
}

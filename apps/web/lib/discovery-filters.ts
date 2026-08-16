export type DiscoverySort = "popular" | "rating" | "newest" | "most-rated";

export interface DiscoveryFilters {
  genreIds: number[];
  originCountry?: string;
  year?: number;
  ratingGte?: number;
  runtimeLte?: number;
  sort: DiscoverySort;
}

export const EMPTY_DISCOVERY_FILTERS: DiscoveryFilters = {
  genreIds: [],
  sort: "popular",
};

export const DISCOVERY_COUNTRIES = [
  ["CN", "中国大陆"],
  ["US", "美国"],
  ["JP", "日本"],
  ["KR", "韩国"],
  ["GB", "英国"],
  ["FR", "法国"],
  ["HK", "中国香港"],
  ["TW", "中国台湾"],
  ["IN", "印度"],
] as const;

const COUNTRY_LABELS = new Map<string, string>(DISCOVERY_COUNTRIES);
const SORT_LABELS: Record<DiscoverySort, string> = {
  popular: "热门优先",
  rating: "评分优先",
  newest: "最新优先",
  "most-rated": "最多评分",
};

type SearchValues = Record<string, string | string[] | undefined>;

function first(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

function validNumber(value: string | undefined, min: number, max: number): number | undefined {
  if (!value) return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= min && parsed <= max ? parsed : undefined;
}

/** URL 是筛选状态的唯一来源；无效或过期值安全忽略，不让分享链接破坏页面。 */
export function parseDiscoveryFilters(values: SearchValues): DiscoveryFilters {
  const sortValue = first(values.sort);
  const sort: DiscoverySort =
    sortValue === "rating" || sortValue === "newest" || sortValue === "most-rated"
      ? sortValue
      : "popular";
  const genres = (first(values.genres) ?? "")
    .split(",")
    .map(Number)
    .filter((value) => Number.isInteger(value) && value > 0);
  const country = first(values.country)?.toUpperCase();
  const year = validNumber(first(values.year), 1874, 2100);
  const ratingGte = validNumber(first(values.rating), 0, 10);
  const runtimeLte = validNumber(first(values.runtime), 1, 600);
  return {
    genreIds: [...new Set(genres)],
    sort,
    ...(country && /^[A-Z]{2}$/.test(country) ? { originCountry: country } : {}),
    ...(year !== undefined ? { year } : {}),
    ...(ratingGte !== undefined ? { ratingGte } : {}),
    ...(runtimeLte !== undefined ? { runtimeLte } : {}),
  };
}

export function discoveryFilterCount(filters: DiscoveryFilters): number {
  return (
    Number(filters.genreIds.length > 0) +
    Number(Boolean(filters.originCountry)) +
    Number(Boolean(filters.year)) +
    Number(filters.ratingGte !== undefined) +
    Number(Boolean(filters.runtimeLte)) +
    Number(filters.sort !== "popular")
  );
}

/** 把 URL 筛选状态转为可扫读标签；类型名由后端本地化清单补齐。 */
export function discoveryFilterLabels(
  filters: DiscoveryFilters,
  genreNames: ReadonlyMap<number, string> = new Map(),
): string[] {
  const labels = filters.genreIds.map((id) => genreNames.get(id)).filter(Boolean) as string[];
  if (filters.genreIds.length > 0 && labels.length === 0) {
    labels.push(`${filters.genreIds.length} 个类型`);
  }
  if (filters.originCountry) {
    labels.push(COUNTRY_LABELS.get(filters.originCountry) ?? filters.originCountry);
  }
  if (filters.year) labels.push(`${filters.year} 年`);
  if (filters.ratingGte !== undefined) labels.push(`${filters.ratingGte} 分以上`);
  if (filters.runtimeLte) labels.push(`${filters.runtimeLte} 分钟以内`);
  if (filters.sort !== "popular") labels.push(SORT_LABELS[filters.sort]);
  return labels;
}

export function discoveryFiltersQuery(filters: DiscoveryFilters, source = "tmdb"): string {
  const params = new URLSearchParams();
  if (source !== "tmdb") params.set("source", source);
  if (filters.genreIds.length > 0) params.set("genres", filters.genreIds.join(","));
  if (filters.originCountry) params.set("country", filters.originCountry);
  if (filters.year) params.set("year", String(filters.year));
  if (filters.ratingGte !== undefined) params.set("rating", String(filters.ratingGte));
  if (filters.runtimeLte) params.set("runtime", String(filters.runtimeLte));
  if (filters.sort !== "popular") params.set("sort", filters.sort);
  return params.toString();
}

export function discoveryFiltersKey(filters: DiscoveryFilters): string {
  return discoveryFiltersQuery(filters);
}

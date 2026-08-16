/** 任务中心公开视图名；查询参数使用同一集合，保证深链不会漂移。 */
export const TASK_CENTER_VIEWS = [
  "all",
  "attention",
  "active",
  "history",
] as const;

export type TaskCenterViewName = (typeof TASK_CENTER_VIEWS)[number];

/** 非法或重复查询值安全回退“全部”，避免 URL 直接控制内部状态。 */
export function taskCenterViewFromQuery(
  value: string | string[] | undefined,
): TaskCenterViewName {
  const candidate = Array.isArray(value) ? value[0] : value;
  return TASK_CENTER_VIEWS.includes(candidate as TaskCenterViewName)
    ? (candidate as TaskCenterViewName)
    : "all";
}

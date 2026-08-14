/** 超过这个窗口的存量资源耗时不再反映订阅链路的实时性，不在详情页展示。 */
export const RESOURCE_TIMING_WINDOW_SECONDS = 7 * 24 * 60 * 60;

export function shouldShowResourceTiming(publishToSubmitSeconds: number | null): boolean {
  return (
    publishToSubmitSeconds == null ||
    publishToSubmitSeconds <= RESOURCE_TIMING_WINDOW_SECONDS
  );
}

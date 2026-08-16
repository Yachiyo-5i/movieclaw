/** 只有已进入救援窗口且仍能定位下载器任务时，提示区才提供立即换种。 */
export function shouldOfferInlineReplacement(task: {
  can_replace: boolean;
  downloader_id: number | null;
}): boolean {
  return task.can_replace && task.downloader_id !== null;
}

import type { Metadata } from "next";

import { TaskCenterView } from "@/components/task-center-view";

export const metadata: Metadata = { title: "任务中心" };

/** 任务中心（/tasks）：统一观察后台 Job、下载器任务与订阅投递。 */
export default function TasksPage() {
  return <TaskCenterView />;
}

import type { Metadata } from "next";

import { TaskCenterView } from "@/components/task-center-view";
import { taskCenterViewFromQuery } from "@/lib/task-center";

export const metadata: Metadata = { title: "任务中心" };

/** 任务中心（/tasks）：统一观察后台 Job、下载器任务与订阅投递。 */
export default async function TasksPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const query = await searchParams;
  const initialView = taskCenterViewFromQuery(query.view);
  return <TaskCenterView key={initialView} initialView={initialView} />;
}

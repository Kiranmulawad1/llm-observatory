"use server";

import { revalidatePath } from "next/cache";
import { api } from "@/lib/api";

/**
 * Server Actions for the review queue.
 *
 * Mutations go here rather than through the read proxy — the proxy is GET-only
 * on purpose, so that a browser can never reach the full authenticated API. An
 * action validates its own input and runs on the server, where the credential
 * lives.
 */

export async function labelItem(
  project: string,
  itemId: string,
  formData: FormData,
): Promise<{ error?: string }> {
  const verdict = String(formData.get("verdict") ?? "");
  if (verdict !== "good" && verdict !== "bad") {
    return { error: "Pick good or bad." };
  }

  const corrected = String(formData.get("corrected_output") ?? "").trim();
  // Caught here as well as server-side, so the reviewer finds out now rather
  // than when promotion fails later: a "bad" example with no expected answer
  // can't be scored by the evaluators you'd run against it.
  if (verdict === "bad" && !corrected) {
    return { error: "A 'bad' verdict needs the answer it should have given." };
  }

  try {
    await api.post(`/projects/${project}/review/${itemId}/label`, {
      verdict,
      reason: String(formData.get("reason") ?? "") || null,
      notes: String(formData.get("notes") ?? "") || null,
      corrected_output: corrected || null,
      labeled_by: String(formData.get("labeled_by") ?? "") || null,
    });
  } catch (e) {
    return { error: e instanceof Error ? e.message : "Could not save the label" };
  }

  revalidatePath(`/${project}/review`);
  return {};
}

export async function skipItem(project: string, itemId: string): Promise<void> {
  await api.post(`/projects/${project}/review/${itemId}/skip`);
  revalidatePath(`/${project}/review`);
}

export async function promoteItems(
  project: string,
  formData: FormData,
): Promise<{ error?: string; version?: number }> {
  const itemIds = formData.getAll("item_ids").map(String);
  const dataset = String(formData.get("dataset") ?? "");

  if (itemIds.length === 0) return { error: "Select at least one labelled item." };
  if (!dataset) return { error: "Pick a dataset to promote into." };

  try {
    const version = await api.post<{ version: number }>(
      `/projects/${project}/review/promote`,
      { item_ids: itemIds, dataset },
    );
    revalidatePath(`/${project}/review`);
    return { version: version.version };
  } catch (e) {
    return { error: e instanceof Error ? e.message : "Could not promote" };
  }
}

// poll-dispatch — trigger the GitHub `poll` workflow via workflow_dispatch.
//
// GitHub's shared cron scheduler is best-effort and skips most */15 slots on
// small public repos (observed: ~1 fire/hour with multi-hour dead zones), while
// manual dispatches run immediately. So Supabase pg_cron calls this function on
// an exact 15-minute clock and it dispatches the workflow like a manual click.
//
// Secrets (Dashboard → Edge Functions → Secrets, or `supabase secrets set`):
//   GH_PAT           required — fine-grained PAT, repo-scoped, Actions: read/write
//   DISPATCH_SECRET  required — shared secret; callers must send it in the
//                    x-dispatch-secret header (JWT verification is off)
//   GH_REPO          optional — owner/repo   (default "Kagwep/catalyst")
//   GH_WORKFLOW      optional — workflow file (default "poll.yml")
//   GH_REF           optional — branch        (default "main")

Deno.serve(async (req: Request) => {
  const json = (body: unknown, status: number) =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });

  const secret = Deno.env.get("DISPATCH_SECRET");
  if (!secret || req.headers.get("x-dispatch-secret") !== secret) {
    return json({ error: "forbidden" }, 403);
  }
  const pat = Deno.env.get("GH_PAT");
  if (!pat) {
    return json({ error: "GH_PAT secret is not set" }, 500);
  }

  const repo = Deno.env.get("GH_REPO") ?? "Kagwep/catalyst";
  const workflow = Deno.env.get("GH_WORKFLOW") ?? "poll.yml";
  const ref = Deno.env.get("GH_REF") ?? "main";

  const resp = await fetch(
    `https://api.github.com/repos/${repo}/actions/workflows/${workflow}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${pat}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "catalyst-poll-dispatch",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref }),
    },
  );

  // GitHub answers 204 No Content on a successful dispatch.
  if (resp.status === 204) {
    return json({ dispatched: true, repo, workflow, ref }, 200);
  }
  const detail = (await resp.text()).slice(0, 500);
  return json({ dispatched: false, github_status: resp.status, detail }, 502);
});

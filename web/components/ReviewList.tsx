"use client";

import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";

import { formatWhen } from "./BriefSection";
import { Spinner, Tag } from "./ui";

export function ReviewList() {
  const reviews = useQuery({ queryKey: ["reviews"], queryFn: api.reviews, refetchInterval: 10_000 });

  return (
    <main className="list-page">
      <header className="list-head">
        <h1>Reviews</h1>
        {reviews.data && reviews.data.length > 0 && <span className="list-count">{reviews.data.length}</span>}
      </header>
      {reviews.isPending && <Spinner label="Loading reviews" />}
      {reviews.isError && <p className="muted">Couldn&apos;t load reviews: {String(reviews.error.message)}</p>}
      {reviews.data?.length === 0 && (
        <p className="muted">
          No briefs yet. Run <code>/pair owner/repo#123</code> in Claude Code to build one.
        </p>
      )}
      {!!reviews.data?.length && (
        <div className="review-rows">
          {reviews.data.map((r) => (
            <a key={r.key} className="review-row" href={`/review/?key=${encodeURIComponent(r.key)}`}>
              <span className="ref" title={r.repo ?? undefined}>
                {r.repo?.split("/").pop() ?? r.key}
                {r.number != null && <span className="num">#{r.number}</span>}
              </span>
              <span className="title">{r.title ?? r.key}</span>
              {r.pending_count > 0 && <Tag tone="caution">{`${r.pending_count} pending`}</Tag>}
              <span className="when">{formatWhen(r.updated_at) ?? ""}</span>
            </a>
          ))}
        </div>
      )}
    </main>
  );
}

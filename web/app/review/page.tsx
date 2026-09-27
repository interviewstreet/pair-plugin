"use client";

import { useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { ReviewList } from "@/components/ReviewList";
import { ReviewPage } from "@/components/ReviewPage";

function ReviewRoute() {
  const key = useSearchParams().get("key");
  return key ? <ReviewPage reviewKey={key} key={key} /> : <ReviewList />;
}

export default function Review() {
  return (
    <Suspense>
      <ReviewRoute />
    </Suspense>
  );
}

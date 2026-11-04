"use client";

import { useReportWebVitals } from "next/web-vitals";

type ReportedMetric = Parameters<Parameters<typeof useReportWebVitals>[0]>[0];

const ENDPOINT = "/api/vitals";

function send(metric: ReportedMetric) {
  const body = JSON.stringify({
    name: metric.name,
    value: metric.value,
    rating: metric.rating,
    id: metric.id,
    navigation_type: metric.navigationType,
    // The pathname and the view only: the query string can carry tokens.
    path: window.location.pathname,
    // Raw `?v=`; the route maps it onto a real view id. Importing the view
    // table here would put the navigation config in every page's bundle.
    view: new URLSearchParams(window.location.search).get("v") || undefined,
  });
  const blob = new Blob([body], { type: "application/json" });
  if (typeof navigator.sendBeacon === "function" && navigator.sendBeacon(ENDPOINT, blob)) return;
  void fetch(ENDPOINT, { method: "POST", body, keepalive: true, headers: { "content-type": "application/json" } }).catch(
    () => {}
  );
}

/** Reports Core Web Vitals to /api/vitals. Renders nothing. */
export function WebVitals() {
  useReportWebVitals(send);
  return null;
}

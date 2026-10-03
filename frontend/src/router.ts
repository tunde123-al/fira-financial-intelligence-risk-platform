import { useEffect, useState } from "react";

// Minimal hash router: "#/customers/CUST-1?x=1" -> { path: ["customers","CUST-1"], query: {x:"1"} }
export interface Route {
  path: string[];
  query: Record<string, string>;
}

function parse(): Route {
  const h = window.location.hash.replace(/^#\/?/, "");
  const [p, q = ""] = h.split("?");
  const query: Record<string, string> = {};
  new URLSearchParams(q).forEach((v, k) => (query[k] = v));
  return { path: p.split("/").filter(Boolean).map(decodeURIComponent), query };
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(parse());
  useEffect(() => {
    const fn = () => setRoute(parse());
    window.addEventListener("hashchange", fn);
    return () => window.removeEventListener("hashchange", fn);
  }, []);
  return route;
}

export function go(path: string): void {
  window.location.hash = path.startsWith("#") ? path : `#${path}`;
}

export function entityHref(type: string, id: string): string {
  switch (type) {
    case "customer":
      return `#/customers/${id}`;
    case "investigation":
      return `#/investigations/${id}`;
    case "account":
    case "device":
    case "merchant":
      return `#/graph?kind=${type}&id=${id}`;
    case "transaction":
      return `#/search?q=${id}`;
    default:
      return "#/";
  }
}

import { proxyGateway } from "@/lib/gateway";

type RouteContext = {
  params: Promise<{ virtualKey: string }>;
};

export async function POST(request: Request, context: RouteContext) {
  const { virtualKey } = await context.params;
  const body = await request.text();
  return proxyGateway(`/admin/users/${encodeURIComponent(virtualKey)}/active`, {
    method: "POST",
    body,
    headers: {
      "Content-Type": request.headers.get("content-type") ?? "application/json",
    },
  });
}

import { proxyGateway } from "@/lib/gateway";

export async function GET(request: Request) {
  const url = new URL(request.url);
  return proxyGateway(`/admin/audit-log${url.search}`);
}

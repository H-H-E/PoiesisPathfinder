import { proxyGateway } from "@/lib/gateway";

export async function GET() {
  return proxyGateway("/admin/users");
}

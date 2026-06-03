function baseUrl(): string {
  return process.env.GATEWAY_BRAIN_URL ?? "http://localhost:8000";
}

function adminHeaders(): HeadersInit {
  const token = process.env.POIESIS_ADMIN_TOKEN;
  if (!token) {
    return {};
  }
  return { "x-admin-token": token };
}

export async function proxyGateway(path: string, init: RequestInit = {}): Promise<Response> {
  const response = await fetch(`${baseUrl()}${path}`, {
    ...init,
    cache: "no-store",
    headers: {
      ...adminHeaders(),
      ...(init.headers ?? {}),
    },
  });
  const body = await response.text();
  return new Response(body, {
    status: response.status,
    headers: {
      "Content-Type": response.headers.get("content-type") ?? "application/json",
    },
  });
}

import { NextRequest, NextResponse } from "next/server";
import { isSameOrigin } from "@/lib/request-origin";

export async function DELETE(request: NextRequest) {
  if (!isSameOrigin(request)) {
    return NextResponse.json({ detail: "Cross-origin requests are not allowed" }, { status: 403 });
  }
  const response = NextResponse.json({ status: "signed_out" });
  for (const name of ["resolveai_customer_access", "resolveai_customer_refresh"]) {
    response.cookies.set(name, "", {
      httpOnly: true,
      sameSite: "strict",
      secure: process.env.NODE_ENV === "production",
      path: "/api/customer-backend",
      maxAge: 0,
    });
  }
  return response;
}

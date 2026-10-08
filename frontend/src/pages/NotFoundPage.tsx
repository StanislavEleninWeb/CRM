import { Link } from "react-router-dom";

import { EmptyState } from "../components/States";

export function NotFoundPage() {
  return (
    <EmptyState title="Page not found">
      <p>
        <Link to="/">Go to Today</Link>
      </p>
    </EmptyState>
  );
}

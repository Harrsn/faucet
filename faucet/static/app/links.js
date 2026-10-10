// Where things open — one place, so a route change is one edit.
export const links = {
  home: "#/",
  shows: "#/shows",
  show: (id, season) => `#/shows/${encodeURIComponent(id)}${season != null ? `/${encodeURIComponent(season)}` : ""}`,
  movies: "#/movies",
  movie: (id) => `#/movies/${encodeURIComponent(id)}`,
  discover: "#/discover",
  calendar: "#/calendar",
  requests: "#/requests",
  activity: "#/activity",
  wanted: "#/wanted",
  indexers: "#/settings/integrations",
  settings: "#/settings/libraries",
  users: "#/settings/users",
};


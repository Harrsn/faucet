// Where things open. Pages the new UI doesn't have yet deep-link into the
// classic UI; as each view lands, its entry here moves to a #/ route.
export const links = {
  home: "#/",
  shows: "/classic#page/shows",
  show: (id) => `/classic#show/${encodeURIComponent(id)}`,
  movies: "/classic#page/movies",
  movie: (id) => `/classic#movie/${encodeURIComponent(id)}`,
  discover: "/classic#page/discover",
  calendar: "/classic#page/calendar",
  requests: "/classic#page/requests",
  activity: "/classic#activity",
  wanted: "/classic#page/dashboard",
  indexers: "/classic#indexers",
  settings: "/classic#settings",
  users: "/classic#users",
};

export const isClassic = (href) => href.startsWith("/classic");

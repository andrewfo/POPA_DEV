// Shared mutable state. ES `export let` gives importers a live read but not the
// ability to reassign; several of these are replaced wholesale on fetch, so we
// export a single const object and mutate its fields — any module can both read
// and update them.
//
//  - centerline: { coords: [[lon,lat],...], stations: [POPA ft,...] } from
//    centerline.geojson; drives station -> lat/lon for the vessel outlines.
//  - berthSta:   berth name -> [sw_station, ne_station] (POPA ft), from geometry.
//                Read by the map (berth popups) AND the timeline (lanes()), so it
//                must be shared. Reassigned wholesale on load, so its object
//                identity also serves as the timeline's lane-cache key.
//  - mooredSog:  SOG (kn) below which a contact reads "stopped"; filled from
//                /config/bbox. The default holds only until that fetch resolves.
//  - vessels:    last-fetched vessel list (id/name/dims), for the vessel edit form.
export const state = {
  centerline: null,
  berthSta: {},
  mooredSog: 0.5,
  vessels: [],
};

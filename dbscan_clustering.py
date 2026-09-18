import math
import json

def haversine(lat1, lon1, lat2, lon2):
    # radius of earth in km
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def region_query(faults, p_idx, eps):
    neighbors = []
    f1 = faults[p_idx]
    lat1, lon1 = f1['lat'], f1['long']
    corridor1 = f1.get('corridor_id')
    
    for i, f2 in enumerate(faults):
        if f1['id'] == f2['id']:
            neighbors.append(i)
            continue
            
        # Only cluster faults on the same corridor
        if corridor1 and f2.get('corridor_id') != corridor1:
            continue
            
        lat2, lon2 = f2['lat'], f2['long']
        if haversine(lat1, lon1, lat2, lon2) <= eps:
            neighbors.append(i)
            
    return neighbors

def run_dbscan(faults_file='faults.json', eps=15.0, min_pts=2):
    """
    Reads active faults and runs DBSCAN clustering algorithm based on geographical proximity.
    """
    try:
        with open(faults_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            faults = data.get('faults', [])
    except Exception as e:
        print(f"Error reading faults.json: {e}")
        return []

    # labels: 0 = unclassified, -1 = noise, >0 = cluster ID
    labels = [0] * len(faults) 
    cluster_id = 0
    
    for i in range(len(faults)):
        if labels[i] != 0:
            continue
            
        neighbors = region_query(faults, i, eps)
        
        if len(neighbors) < min_pts:
            labels[i] = -1
        else:
            cluster_id += 1
            labels[i] = cluster_id
            
            # expand cluster
            i_idx = 0
            while i_idx < len(neighbors):
                neighbor_p = neighbors[i_idx]
                
                if labels[neighbor_p] == -1:
                    labels[neighbor_p] = cluster_id
                elif labels[neighbor_p] == 0:
                    labels[neighbor_p] = cluster_id
                    new_neighbors = region_query(faults, neighbor_p, eps)
                    if len(new_neighbors) >= min_pts:
                        # Append only new neighbors
                        for n in new_neighbors:
                            if n not in neighbors:
                                neighbors.append(n)
                
                i_idx += 1
                
    # Group faults by assigned cluster
    clusters = {}
    for i, label in enumerate(labels):
        if label > 0:
            if label not in clusters:
                clusters[label] = []
            clusters[label].append(faults[i])
            
    # Format cluster outputs
    formatted_clusters = []
    for cid, items in clusters.items():
        if len(items) < 2:
            continue # Only interested in joint possessions
            
        max_dur = max([item.get('required_block_duration_min', 0) for item in items])
        total_dur = sum([item.get('required_block_duration_min', 0) for item in items])
        time_saved = total_dur - max_dur
        
        depts = list(set([item.get('department') for item in items]))
        
        # Sort items by chainage or ID for stable output
        items.sort(key=lambda x: x.get('id', ''))
        
        # Get overall bounding box or average location
        avg_lat = sum(f['lat'] for f in items) / len(items)
        avg_long = sum(f['long'] for f in items) / len(items)
        
        CORRIDOR_NAMES = {
            "hwh_bwn_main": "Howrah–Barddhaman Main Line",
            "bly_bwn_asn_trunk": "Bally–Barddhaman–Asansol Trunk",
            "bwn_asn_trunk": "Bally–Barddhaman–Asansol Trunk",
            "sdah_knj_line": "Sealdah–Krishnanagar Line",
            "hwh_sgkh_line": "Howrah–Saktigarh Line",
            "hwh_bwn_chord": "Howrah–Barddhaman Chord Line",
            "hwh_bwn_asn_line": "Howrah–Barddhaman Main Line"
        }
        cid_str = items[0].get("corridor_id", "Unknown")
        corr_name = CORRIDOR_NAMES.get(cid_str, cid_str.replace("_", " ").title() if cid_str else "Main Line")
        
        formatted_clusters.append({
            "cluster_id": f"MB-AUTO-{cid}",
            "corridor_id": cid_str,
            "corridor_name": corr_name,
            "faults": items,
            "joint_possession_window_min": max_dur,
            "total_independent_time_min": total_dur,
            "time_saved_min": time_saved,
            "departments": depts,
            "center_lat": avg_lat,
            "center_long": avg_long
        })
        
    return formatted_clusters

if __name__ == "__main__":
    clusters = run_dbscan()
    print(json.dumps(clusters, indent=2))

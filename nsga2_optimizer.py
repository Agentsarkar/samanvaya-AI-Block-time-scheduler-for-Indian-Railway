import random
import math
from typing import List, Dict, Any
from train_position_calculator import parse_hhmm, sort_stops_chronologically
CORRIDOR_NAMES = {
    "hwh_bwn_main": "Howrah–Barddhaman Main Line",
    "bly_bwn_asn_trunk": "Bally–Barddhaman–Asansol Trunk",
    "bwn_asn_trunk": "Bally–Barddhaman–Asansol Trunk",
    "sdah_knj_line": "Sealdah–Krishnanagar Line",
    "hwh_sgkh_line": "Howrah–Saktigarh Line",
    "hwh_bwn_chord": "Howrah–Barddhaman Chord Line",
    "hwh_bwn_asn_line": "Howrah–Barddhaman Main Line"
}

class NSGA2Solution:
    def __init__(self, num_clusters: int):
        # Chromosome: start time (minutes) for each cluster
        self.chromosome = [random.randint(0, 1440 - 30) for _ in range(num_clusters)]
        self.objectives = [0.0, 0.0, 0.0]  # [f1: delays, f2: rush hour penalty, f3: makespan]
        self.rank = 0
        self.crowding_distance = 0.0

def dominates(s1: NSGA2Solution, s2: NSGA2Solution) -> bool:
    better_in_all = True
    strictly_better_in_one = False
    for i in range(len(s1.objectives)):
        if s1.objectives[i] > s2.objectives[i]:
            better_in_all = False
            break
        elif s1.objectives[i] < s2.objectives[i]:
            strictly_better_in_one = True
    return better_in_all and strictly_better_in_one

def fast_non_dominated_sort(population: List[NSGA2Solution]) -> List[List[NSGA2Solution]]:
    fronts = [[]]
    for p in population:
        p.domination_count = 0
        p.dominated_solutions = []
        for q in population:
            if dominates(p, q):
                p.dominated_solutions.append(q)
            elif dominates(q, p):
                p.domination_count += 1
        if p.domination_count == 0:
            p.rank = 1
            fronts[0].append(p)

    i = 0
    while len(fronts[i]) > 0:
        next_front = []
        for p in fronts[i]:
            for q in p.dominated_solutions:
                q.domination_count -= 1
                if q.domination_count == 0:
                    q.rank = i + 2
                    next_front.append(q)
        i += 1
        fronts.append(next_front)
    return fronts[:-1]

def crowding_distance_assignment(front: List[NSGA2Solution]):
    l = len(front)
    if l == 0:
        return
    for p in front:
        p.crowding_distance = 0
    
    num_objs = len(front[0].objectives)
    for m in range(num_objs):
        front.sort(key=lambda x: x.objectives[m])
        front[0].crowding_distance = float('inf')
        front[-1].crowding_distance = float('inf')
        f_max = front[-1].objectives[m]
        f_min = front[0].objectives[m]
        if f_max == f_min:
            continue
        for i in range(1, l - 1):
            front[i].crowding_distance += (front[i+1].objectives[m] - front[i-1].objectives[m]) / (f_max - f_min)

def evaluate(solution: NSGA2Solution, clusters: List[Dict], trains: List[Dict], day_short: str):
    total_delay = 0.0
    rush_penalty = 0.0
    max_end_time = 0.0
    
    for i, c in enumerate(clusters):
        start_min = solution.chromosome[i]
        dur_min = c.get('joint_possession_window_min', 60)
        end_min = start_min + dur_min
        corridor_id = c.get('corridor_id')
        
        # Calculate makespan
        if end_min > max_end_time:
            max_end_time = end_min
            
        # Rush hour penalty (08:00-11:00 and 17:00-20:00)
        # 480 to 660, 1020 to 1200
        overlap_rush1 = max(0, min(end_min, 660) - max(start_min, 480))
        overlap_rush2 = max(0, min(end_min, 1200) - max(start_min, 1020))
        rush_penalty += (overlap_rush1 + overlap_rush2)
        
        # Calculate train delays
        for t in trains:
            if t.get('corridor_id') != corridor_id:
                continue
            run_days = t.get("run_days", [])
            run_norm = [d[:3].lower() for d in run_days]
            if run_norm and day_short not in run_norm:
                continue
                
            # Get train window
            ordered, _ = sort_stops_chronologically(t["corridor_stops"])
            if not ordered:
                continue
                
            first = ordered[0]
            last = ordered[-1]
            t_dep = parse_hhmm(first.get("departure") or first.get("arrival"))
            t_arr = parse_hhmm(last.get("arrival") or last.get("departure"))
            
            if t_dep is None or t_arr is None:
                continue
                
            if t_arr < t_dep:
                # Crosses midnight, simplify by bounding to end of day
                t_arr = 1440
                
            overlap = max(0, min(end_min, t_arr) - max(start_min, t_dep))
            if overlap > 0:
                total_delay += overlap

    solution.objectives = [total_delay, rush_penalty, max_end_time]

def tournament_selection(pop: List[NSGA2Solution], k=2) -> NSGA2Solution:
    best = random.choice(pop)
    for _ in range(k - 1):
        ind = random.choice(pop)
        if ind.rank < best.rank:
            best = ind
        elif ind.rank == best.rank and ind.crowding_distance > best.crowding_distance:
            best = ind
    return best

def crossover_and_mutation(p1: NSGA2Solution, p2: NSGA2Solution, num_clusters: int) -> NSGA2Solution:
    child = NSGA2Solution(num_clusters)
    for i in range(num_clusters):
        # SBX crossover-like simple blending
        if random.random() < 0.5:
            child.chromosome[i] = p1.chromosome[i]
        else:
            child.chromosome[i] = p2.chromosome[i]
            
        # Mutation
        if random.random() < 0.2:
            shift = random.randint(-60, 60)
            child.chromosome[i] = max(0, min(1440 - 30, child.chromosome[i] + shift))
    return child

def run_nsga2(clusters: List[Dict], trains: List[Dict], day_short: str, pop_size=50, max_gen=50) -> List[Dict]:
    num_clusters = len(clusters)
    if num_clusters == 0:
        return []

    # Initialize population
    population = [NSGA2Solution(num_clusters) for _ in range(pop_size)]
    for p in population:
        evaluate(p, clusters, trains, day_short)
        
    fronts = fast_non_dominated_sort(population)
    if len(fronts) > 0:
        for f in fronts:
            crowding_distance_assignment(f)
        
    # Evolution loop
    for gen in range(max_gen):
        offspring = []
        while len(offspring) < pop_size:
            p1 = tournament_selection(population)
            p2 = tournament_selection(population)
            child = crossover_and_mutation(p1, p2, num_clusters)
            evaluate(child, clusters, trains, day_short)
            offspring.append(child)
            
        population.extend(offspring)
        fronts = fast_non_dominated_sort(population)
        
        new_pop = []
        for front in fronts:
            crowding_distance_assignment(front)
            if len(new_pop) + len(front) <= pop_size:
                new_pop.extend(front)
            else:
                front.sort(key=lambda x: x.crowding_distance, reverse=True)
                new_pop.extend(front[:pop_size - len(new_pop)])
                break
        population = new_pop

    # Get pareto frontier (Rank 1)
    fronts = fast_non_dominated_sort(population)
    pareto_front = fronts[0]
    
    # We want to return ~3 diverse solutions to the UI
    # Sort by Objective 1 (Delay)
    pareto_front.sort(key=lambda x: x.objectives[0])
    
    unique_solutions = []
    seen_objs = set()
    for s in pareto_front:
        key = (s.objectives[0], s.objectives[1], s.objectives[2])
        if key not in seen_objs:
            seen_objs.add(key)
            unique_solutions.append(s)
            
    if len(unique_solutions) <= 3:
        selected = unique_solutions
    else:
        # Pick extremes and a middle one
        selected = [unique_solutions[0], unique_solutions[len(unique_solutions)//2], unique_solutions[-1]]

    results = []
    names = ["Zero/Min Delay Schedule", "Balanced Optimal Schedule", "Fastest Completion Schedule"]
    
    # In case there's only 1 or 2 unique solutions
    for i, sol in enumerate(selected):
        name = names[i] if i < len(names) else f"Optimal Schedule {i+1}"
        # Format the chromosome back into time strings for the UI
        schedule_details = []
        for j, c in enumerate(clusters):
            start_min = sol.chromosome[j]
            dur = c.get('joint_possession_window_min', 60)
            end_min = start_min + dur
            hh_s = start_min // 60
            mm_s = start_min % 60
            hh_e = end_min // 60
            mm_e = end_min % 60
            cid = c.get("corridor_id", "")
            corr_name = c.get("corridor_name") or CORRIDOR_NAMES.get(cid, cid.replace("_", " ").title() if cid else "Main Corridor")
            schedule_details.append({
                "cluster_id": c.get("cluster_id"),
                "corridor_id": cid,
                "corridor_name": corr_name,
                "num_faults": len(c.get("faults", [])),
                "departments": c.get("departments", []),
                "start_time": f"{hh_s:02d}:{mm_s:02d}",
                "end_time": f"{hh_e:02d}:{mm_e:02d}",
                "duration": dur
            })
            
        results.append({
            "option_name": name,
            "total_delay_min": sol.objectives[0],
            "rush_hour_penalty": sol.objectives[1],
            "makespan_min": sol.objectives[2],
            "schedule": schedule_details
        })

    return results

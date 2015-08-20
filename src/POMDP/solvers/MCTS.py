__author__ = 'patrickemami'

import time
import random
import numpy as np
from POMDP.belief_tree import BeliefTree
from actionselection import action_selectors
from POMDP.statistic import Statistic
from console import *

module = "MCTS"


class MCTS(object):
    """
    Monte-Carlo Tree Search implementation, from POMCP
    """

    """
    Dimensions for the fast-UCB table
    """
    UCB_N = 10000
    UCB_n = 100

    def __init__(self, solver, model):
        self.solver = solver
        self.model = model
        self.policy = BeliefTree(solver) # Search Tree
        self.peak_tree_depth = 0
        self.disable_tree = False
        self.tree_depth_stats = Statistic("Tree Depth")
        self.rollout_depth_stats = Statistic("Rollout Depth")
        self.total_reward_stats = Statistic("Total Reward")
        self.step_size = self.model.sys_cfg["step_size"]

        # Solver owns Histories, the collection of History Sequences.
        # There is one sequence per run of the MCTS algorithm
        self.history = self.solver.histories.create_sequence()

        # Pre-calculate UCB values for a speed-up
        self.fast_UCB = [[None for _ in range(MCTS.UCB_n)] for _ in range(MCTS.UCB_N)]

        for N in range(MCTS.UCB_N):
            for n in range(MCTS.UCB_n):
                if n is 0:
                    self.fast_UCB[N][n] = np.inf
                else:
                    self.fast_UCB[N][n] = model.sys_cfg["ucb_coefficient"] * np.sqrt(np.log(N + 1)/n)

        # Initialize the Belief Tree
        self.reset()

    def reset(self):
        # Initialize policy root stuff
        self.policy.reset()
        self.policy.initialize()

        # generate state particles for root node belief state estimation
        # This is for simulation
        for i in range(self.model.sys_cfg["num_start_states"]):
            particle = self.model.sample_an_init_state()
            self.policy.root.state_particles.append(particle)

    def clear_stats(self):
        self.total_reward_stats.clear()
        self.tree_depth_stats.clear()
        self.rollout_depth_stats.clear()

    def find_fast_ucb(self, total_visit_count, action_map_entry_visit_count, log_n):
        assert self.fast_UCB is not None
        if total_visit_count < MCTS.UCB_N and action_map_entry_visit_count < MCTS.UCB_n:
            return self.fast_UCB[int(total_visit_count)][int(action_map_entry_visit_count)]

        if action_map_entry_visit_count == 0:
            return np.inf
        else:
            return self.model.sys_cfg["ucb_coefficient"] * np.sqrt(log_n/action_map_entry_visit_count)

    def select_action(self):
        if self.disable_tree:
            self.rollout_search()
        else:
            self.UCT_search()
        return action_selectors.ucb_action(self, self.policy.root, True)

    def update(self, step_result):

        # Update the Simulator with the Step Result
        # This is important in case there are certain actions that change the state of the simulator
        self.model.update(step_result)

        child_belief_node = self.policy.root.get_child(step_result.action, step_result.observation)

        ''' --- DAMAGE CONTROL --- '''
        ''' ================================================================================================'''
        # If the child_belief_node is None because the step result randomly produced a different observation,
        # grab any of the beliefs extending from the belief node's action node
        if child_belief_node is None:
            action_node = self.policy.root.action_map.get_action_node(step_result.action)
            if action_node is None:
                # I grabbed a child belief node that doesn't have an action node. Use rollout from here on out.
                print "Child belief node None"
                return True

            obs_mapping_entries = action_node.observation_map.child_map.values()

            for entry in obs_mapping_entries:
                if entry.child_node is not None:
                    child_belief_node = entry.child_node
                    print "Had to grab nearest belief node...uncertainty introduced"
                    break
        ''' ================================================================================================'''

        # Extend the history sequence
        new_hist_entry = self.history.add_entry()
        new_hist_entry.reward = step_result.reward
        new_hist_entry.action = step_result.action
        new_hist_entry.observation = step_result.observation
        new_hist_entry.register_entry(new_hist_entry, None, step_result.next_state)

        # If the new root does not yet have the max possible number of particles add some more
        if child_belief_node.state_particles.__len__() < self.model.sys_cfg["max_particle_count"]:

            num_to_add = self.model.sys_cfg["max_particle_count"] - child_belief_node.state_particles.__len__()

            # Generate particles for the new root node
            child_belief_node.state_particles += self.model.generate_particles(self.policy.root, step_result.action,
                                            step_result.observation, num_to_add,
                                            self.policy.root.state_particles)

            # If that failed, attempt to create a new state particle set
            if child_belief_node.state_particles.__len__() == 0:
                child_belief_node.state_particles += self.model.generate_particles_uninformed(self.policy.root, step_result.action,
                                                                                    step_result.observation,
                                                                                    self.model.sys_cfg["min_particle_count"])

        # Failed to continue search- ran out of particles
        if child_belief_node is None or child_belief_node.state_particles.__len__() == 0:
            print "Couldn't refill particles!!!"
            return True

        # delete old tree and set the new root
        start_time = time.time()
        self.policy.prune_siblings(child_belief_node)
        elapsed = time.time() - start_time
        print "Time spent pruning = ", str(elapsed)
        self.policy.root = child_belief_node
        return False

    ''' --------------- Rollout Search --------------'''
    '''
    At each node, examine all legal actions and choose the actions with
    the highest evaluation
    '''
    def rollout_search(self):
        for i in range(self.model.sys_cfg["num_sims"]):
            state = self.policy.root.sample_particle()
            legal_actions = self.policy.root.data.generate_legal_actions()
            action = legal_actions[i % legal_actions.__len__()]

            # model#generate_step casts the variable action from an int to the proper DiscreteAction subclass type
            step_result, is_legal = self.model.generate_step(state, action)

            if not step_result.is_terminal:
                child_node, added = self.policy.root.create_or_get_child(step_result.action, step_result.observation)
                child_node.state_particles.append(step_result.next_state)
                delayed_reward = self.rollout(step_result.next_state, child_node.data.generate_legal_actions())
            else:
                delayed_reward = 0

            # TODO Might want to subtract out the current mean_q_value
            total_reward = (step_result.reward + self.model.sys_cfg["discount"] * delayed_reward) * self.step_size
            action_mapping_entry = self.policy.root.action_map.get_entry(step_result.action.bin_number)
            assert action_mapping_entry is not None

            action_mapping_entry.update_visit_count(1.0)
            action_mapping_entry.update_q_value(total_reward)

    def rollout(self, start_state, starting_legal_actions):

        legal_actions = list(starting_legal_actions)
        state = start_state.copy()
        is_terminal = False
        total_reward = 0.0
        discount = 1.0
        num_steps = 0

        while num_steps < self.model.sys_cfg["maximum_depth"] and not is_terminal:
            legal_action = random.choice(legal_actions)
            step_result, is_legal = self.model.generate_step(state, legal_action)
            is_terminal = step_result.is_terminal
            total_reward += step_result.reward * discount
            discount *= self.model.sys_cfg["discount"]
            # advance to next state
            state = step_result.next_state
            # generate new set of legal actions from the new state
            legal_actions = self.model.get_legal_actions(state)
            num_steps += 1

        self.rollout_depth_stats.add(num_steps)
        return total_reward

    ''' --------------- Multi-Armed Bandit Search -------------- '''
    def UCT_search(self):
        """
        Expands the root node via random simulations
        :return:
        """
        start_time = time.time()

        self.clear_stats()
        # Create a snapshot of the current information state
        initial_root_data = self.policy.root.data.copy()

        for i in range(self.model.sys_cfg["num_sims"]):
            # Reset the Simulator
            self.model.reset()

            # Reset the root node to the information state at the beginning of the UCT Search
            # After each simulation
            self.policy.root.data = initial_root_data.copy()

            state = self.policy.root.sample_particle()
            # Tree depth, which increases with each recursive step
            tree_depth = 0
            self.peak_tree_depth = 0

            console(3, module + ".UCT_search", "Starting simulation at random state = " + state.to_string())

            # initiate
            total_reward = self.simulate_node(state, self.policy.root, tree_depth, start_time)

            self.total_reward_stats.add(total_reward)
            self.tree_depth_stats.add(self.peak_tree_depth)

            console(3, module + ".UCT_search", "Total reward = " + str(total_reward))

        # Reset the information state back to the state it was in before the simulations occurred for consistency
        self.policy.root.data = initial_root_data

        console_no_print(3, self.tree_depth_stats.show)
        console_no_print(3, self.rollout_depth_stats.show)
        console_no_print(3, self.total_reward_stats.show)

    def simulate_node(self, state, belief_node, tree_depth, start_time):

        # Time expired
        if time.time() - start_time > self.model.sys_cfg["action_selection_time_out"]:
            return 0

        action = action_selectors.ucb_action(self, belief_node, False)

        self.peak_tree_depth = tree_depth

        # Search horizon reached
        if tree_depth >= self.model.sys_cfg["maximum_depth"]:
            console(4, module + ".simulate_node", "Search horizon reached, getting tf outta here")
            return 0

        if tree_depth == 1:
            # Add a state particle with the new state
            if belief_node.state_particles.__len__() < self.model.sys_cfg["max_particle_count"]:
                belief_node.state_particles.append(state)

        # Q value
        total_reward = self.step_node(belief_node, state, action, tree_depth, start_time)
        # Add RAVE ?
        return total_reward

    def step_node(self, belief_node, state, action, tree_depth, start_time):

        # Time expired
        if time.time() - start_time > self.model.sys_cfg["action_selection_time_out"]:
            return 0

        delayed_reward = 0

        step_result, is_legal = self.model.generate_step(state, action)

        console(4, module + ".step_node", "Step Result.Action = " + step_result.action.to_string())
        console(4, module + ".step_node", "Step Result.Observation = " + step_result.observation.to_string())
        console(4, module + ".step_node", "Step Result.Next_State = " + step_result.next_state.to_string())
        console(4, module + ".step_node", "Step Result.Reward = " + str(step_result.reward))

        if not step_result.is_terminal:
            child_belief_node, added = belief_node.create_or_get_child(action, step_result.observation)

            if child_belief_node is not None:
                tree_depth += 1
                delayed_reward = self.simulate_node(step_result.next_state, child_belief_node, tree_depth, start_time)
            else:
                delayed_reward = self.rollout(state)
            tree_depth -= 1
        else:
            console(3, module + ".step_node", "Reached terminal state.")

        # TODO try subtracting out current Q value for variance-control purposes
        # delayed_reward is "Q maximal"
        q_value = (step_result.reward + self.model.sys_cfg["discount"] * delayed_reward) * self.step_size

        belief_node.action_map.get_entry(action.bin_number).update_visit_count(1)
        belief_node.action_map.get_entry(action.bin_number).update_q_value(q_value)

        return q_value


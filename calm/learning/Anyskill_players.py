# Copyright (c) 2018-2022, NVIDIA Corporation
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its
#    contributors may be used to endorse or promote products derived from
#    this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import copy
from gym import spaces
import numpy as np
import os
import torch
import yaml
import threading
from rl_games.algos_torch import players

import learning.common_player as common_player
import learning.calm_players as calm_players
import learning.calm_models as calm_models
import learning.calm_network_builder as calm_network_builder
from utils import anyskill

skill_command = "put up your hand"


class AnyskillPlayer(common_player.CommonPlayer):
    def __init__(self, config):
        with open(os.path.join(os.getcwd(), config['llc_config']), 'r') as f:
            llc_config = yaml.load(f, Loader=yaml.SafeLoader)
            llc_config_params = llc_config['params']
            self._latent_dim = llc_config_params['config']['latent_dim']

        super().__init__(config)

        self._task_size = self.env.task.get_task_obs_size()

        self._llc_steps = config['llc_steps']
        llc_checkpoint = config['llc_checkpoint']
        assert (llc_checkpoint != "")
        self._build_llc(llc_config_params, llc_checkpoint)

        self._target_motion_index = torch.zeros((self.env.task.num_envs, 1), dtype=torch.long, device=self.device)
        self.anyskill = anyskill.anytest()
        self.text_encoder = anyskill.TextToFeature()
        self.text_latent = self.text_encoder.encode_texts([skill_command])
        self.print_stats = False
        self.skill_command = skill_command

        return

    def get_action(self, obs_dict, is_determenistic=False):
        obs = obs_dict['obs']

        if len(obs.size()) == len(self.obs_shape):
            obs = obs.unsqueeze(0)
        proc_obs = self._preproc_obs(obs)
        input_dict = {
            'is_train': False,
            'prev_actions': None,
            'obs': proc_obs,
            'rnn_states': self.states
        }
        with torch.no_grad():
            res_dict = self.model(input_dict)
        mu = res_dict['mus']
        action = res_dict['actions']
        self.states = res_dict['rnn_states']
        if is_determenistic:
            current_action = mu
        else:
            current_action = action
        current_action = torch.squeeze(current_action.detach())
        clamped_actions = torch.clamp(current_action, -1.0, 1.0)

        return clamped_actions

    def run_anyskill(self):
        n_games = self.games_num
        render = self.render_env
        n_game_life = self.n_game_life
        is_determenistic = self.is_determenistic
        sum_rewards = 0
        sum_steps = 0
        sum_game_res = 0
        n_games = n_games * n_game_life
        games_played = 0
        has_masks = False
        has_masks_func = getattr(self.env, "has_action_mask", None) is not None

        op_agent = getattr(self.env, "create_agent", None)
        if op_agent:
            agent_inited = True

        if has_masks_func:
            has_masks = self.env.has_action_mask()

        need_init_rnn = self.is_rnn
        for _ in range(n_games):
            if games_played >= n_games:
                break

            obs_dict = self.env_reset()
            batch_size = 1
            if len(obs_dict['obs'].size()) > len(self.obs_shape):
                batch_size = obs_dict['obs'].size()[0]
            self.batch_size = batch_size

            if need_init_rnn:
                self.init_rnn()
                need_init_rnn = False

            cr = torch.zeros(batch_size, dtype=torch.float32)
            steps = torch.zeros(batch_size, dtype=torch.float32)

            print_game_res = False

            done_indices = []

            for n in range(self.max_steps):
                self.obs = self.env_reset(done_indices)
                obs = self.obs['obs']

                global skill_command

                if skill_command != self.skill_command:
                    self.skill_command = skill_command
                    self.text_latent = self.text_encoder.encode_texts([skill_command])

                obs[..., self.obs_shape[0] - self._task_size:][:] = self.text_latent
                action = self.get_action(self.obs, is_determenistic)
                obs_dict, r, done, info = self.env_step(action)
                cr += r
                steps += 1

                self._post_step(info)

                if render:
                    self.env.render(mode='human')
                    time.sleep(self.render_sleep)

                all_done_indices = done.nonzero(as_tuple=False)
                done_indices = all_done_indices[::self.num_agents]
                done_count = len(done_indices)
                games_played += done_count

                if done_count > 0:
                    if self.is_rnn:
                        for s in self.states:
                            s[:, all_done_indices, :] = s[:, all_done_indices, :] * 0.0

                    cur_rewards = cr[done_indices].sum().item()
                    cur_steps = steps[done_indices].sum().item()

                    cr = cr * (1.0 - done.float())
                    steps = steps * (1.0 - done.float())
                    sum_rewards += cur_rewards
                    sum_steps += cur_steps

                    game_res = 0.0
                    if isinstance(info, dict):
                        if 'battle_won' in info:
                            print_game_res = True
                            game_res = info.get('battle_won', 0.5)
                        if 'scores' in info:
                            print_game_res = True
                            game_res = info.get('scores', 0.5)
                    if self.print_stats:
                        if print_game_res:
                            print('reward:', cur_rewards / done_count, 'steps:', cur_steps / done_count, 'w:', game_res)
                        else:
                            with open("./output/hrl_reward.txt", "a") as f:
                                f.write(str(cur_rewards / done_count) + "\n")
                                f.close
                            print('reward:', cur_rewards / done_count, 'steps:', cur_steps / done_count)

                    sum_game_res += game_res
                    if batch_size // self.num_agents == 1 or games_played >= n_games:
                        break

                done_indices = done_indices[:, 0]

        # # print(sum_rewards)
        # if print_game_res:
        #     print('av reward:', sum_rewards / games_played * n_game_life, 'av steps:', sum_steps / games_played * n_game_life, 'winrate:', sum_game_res / games_played * n_game_life)
        # else:
        #     print('av reward:', sum_rewards / games_played * n_game_life, 'av steps:', sum_steps / games_played * n_game_life)

        return

    def env_step(self, action):
        if not self.is_tensor_obses:
            action = action.cpu().numpy()

        obs = self.obs['obs']
        rewards = 0.0
        done_count = 0.0
        disc_rewards = 0.0
        for t in range(self._llc_steps):
            llc_actions = self._compute_llc_action(obs, action)
            obs, curr_rewards, curr_dones, infos = self.env.step(llc_actions)

            obs[..., self.obs_shape[0] - self._task_size:][:] = self.text_latent

            rewards += curr_rewards
            done_count += curr_dones

            amp_obs = infos['amp_obs']
            curr_disc_reward = self._calc_disc_reward(amp_obs)
            curr_disc_reward = curr_disc_reward[0, 0].cpu().numpy()
            disc_rewards += curr_disc_reward

        rewards /= self._llc_steps
        dones = torch.zeros_like(done_count)
        dones[done_count > 0] = 1.0

        disc_rewards /= self._llc_steps

        if isinstance(obs, dict):
            obs = obs['obs']
        if obs.dtype == np.float64:
            obs = np.float32(obs)
        if self.value_size > 1:
            rewards = rewards[0]
        if self.is_tensor_obses:
            return obs, rewards.cpu(), dones.cpu(), infos
        else:
            if np.isscalar(dones):
                rewards = np.expand_dims(np.asarray(rewards), 0)
                dones = np.expand_dims(np.asarray(dones), 0)
            return torch.from_numpy(obs).to(self.device), torch.from_numpy(rewards), torch.from_numpy(dones), infos

    def _build_llc(self, config_params, checkpoint_file):
        network_params = config_params['network']

        network_builder = calm_network_builder.CALMBuilder()

        network_builder.load(network_params)

        network = calm_models.ModelCALMContinuous(network_builder)

        llc_agent_config = self._build_llc_agent_config(config_params, network)

        self._llc_agent = calm_players.CALMPlayer(llc_agent_config)

        self._llc_agent.restore(checkpoint_file)
        print("Loaded LLC checkpoint from {:s}".format(checkpoint_file))
        return

    def _build_llc_agent_config(self, config_params, network):
        llc_env_info = copy.deepcopy(self.env_info)
        obs_space = llc_env_info['observation_space']
        obs_size = obs_space.shape[0]
        obs_size -= self._task_size
        llc_env_info['observation_space'] = spaces.Box(obs_space.low[:obs_size], obs_space.high[:obs_size])
        llc_env_info['amp_observation_space'] = self.env.amp_observation_space.shape
        llc_env_info['num_envs'] = self.env.task.num_envs
        llc_env_info['num_amp_obs_steps'] = self.env.task._num_amp_obs_steps

        config = config_params['config']
        config['network'] = network
        config['env_info'] = llc_env_info
        config['env'] = self.env

        return config

    def _setup_action_space(self):
        super()._setup_action_space()
        self.actions_num = self._latent_dim
        return

    def _compute_llc_action(self, obs, actions):
        llc_obs = self._extract_llc_obs(obs)
        processed_obs = self._llc_agent._preproc_obs(llc_obs)

        z = torch.nn.functional.normalize(actions, dim=-1)
        mu, _ = self._llc_agent.model.a2c_network.eval_actor(processed_obs, z)
        llc_action = players.rescale_actions(self.actions_low, self.actions_high, torch.clamp(mu, -1.0, 1.0))

        return llc_action

    def _extract_llc_obs(self, obs):
        obs_size = obs.shape[-1]
        llc_obs = obs[..., :obs_size - self._task_size]
        return llc_obs

    def _calc_disc_reward(self, amp_obs):
        disc_reward = self._llc_agent._calc_disc_rewards(amp_obs)
        return disc_reward

    def get_skill_command(self):
        global skill_command
        while True:
            inputs = input("please input the command: ")
            skill_command = inputs

    def run(self):
        skill_test = threading.Thread(target=self.get_skill_command)
        skill_test.start()
        self.run_anyskill()
        return
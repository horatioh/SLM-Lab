from abc import ABC, abstractmethod
from gym import spaces
from slm_lab.lib import logger, util
from slm_lab.lib.decorator import lab_api
import numpy as np
import pydash as ps
import time

logger = logger.get_logger(__name__)


def set_gym_space_attr(gym_space):
    '''Set missing gym space attributes for standardization'''
    if isinstance(gym_space, spaces.Box):
        setattr(gym_space, 'is_discrete', False)
    elif isinstance(gym_space, spaces.Discrete):
        setattr(gym_space, 'is_discrete', True)
        setattr(gym_space, 'low', 0)
        setattr(gym_space, 'high', gym_space.n)
    elif isinstance(gym_space, spaces.MultiBinary):
        setattr(gym_space, 'is_discrete', True)
        setattr(gym_space, 'low', np.full(gym_space.n, 0))
        setattr(gym_space, 'high', np.full(gym_space.n, 2))
    elif isinstance(gym_space, spaces.MultiDiscrete):
        setattr(gym_space, 'is_discrete', True)
        setattr(gym_space, 'low', np.zeros_like(gym_space.nvec))
        setattr(gym_space, 'high', np.array(gym_space.nvec))
    else:
        raise ValueError('gym_space not recognized')


class Clock:
    '''Clock class for each env and space to keep track of relative time. Ticking and control loop is such that reset is at t=0 and epi=0'''

    def __init__(self, max_frame=int(1e7), clock_speed=1):
        self.max_frame = max_frame
        self.clock_speed = int(clock_speed)
        self.reset()

    def reset(self):
        self.t = 0
        self.frame = 0  # i.e. total_t
        self.epi = 0
        self.start_wall_t = time.time()
        self.wall_t = 0
        self.batch_size = 1  # multiplier to accurately count opt steps
        self.opt_step = 0  # count the number of optimizer updates

    def load(self, train_df):
        '''Load clock from the last row of body.train_df'''
        last_row = train_df.iloc[-1]
        last_clock_vals = ps.pick(last_row, *['epi', 't', 'wall_t', 'opt_step', 'frame'])
        util.set_attr(self, last_clock_vals)
        self.start_wall_t -= self.wall_t  # offset elapsed wall_t

    def get(self, unit='frame'):
        return getattr(self, unit)

    def get_elapsed_wall_t(self):
        '''Calculate the elapsed wall time (int seconds) since self.start_wall_t'''
        return int(time.time() - self.start_wall_t)

    def set_batch_size(self, batch_size):
        self.batch_size = batch_size

    def tick(self, unit='t'):
        if unit == 't':  # timestep
            self.t += self.clock_speed
            self.frame += self.clock_speed
            self.wall_t = self.get_elapsed_wall_t()
        elif unit == 'epi':  # episode, reset timestep
            self.epi += 1
            self.t = 0
        elif unit == 'opt_step':
            self.opt_step += self.batch_size
        else:
            raise KeyError


class BaseEnv(ABC):
    '''
    The base Env class with API and helper methods. Use this to implement your env class that is compatible with the Lab APIs

    e.g. env_spec
    "env": [{
        "name": "PongNoFrameskip-v4",
        "frame_op": "concat",
        "frame_op_len": 4,
        "normalize_state": false,
        "reward_scale": "sign",
        "num_envs": 8,
        "max_t": null,
        "max_frame": 1e7
    }],
    '''

    def __init__(self, spec):
        self.env_spec = spec['env'][0]  # idx 0 for single-env
        # set default
        util.set_attr(self, dict(
            eval_frequency=10000,
            log_frequency=10000,
            frame_op=None,
            frame_op_len=None,
            image_downsize=(84, 84),
            normalize_state=False,
            reward_scale=None,
            num_envs=1,
        ))
        util.set_attr(self, spec['meta'], [
            'eval_frequency',
            'log_frequency',
        ])
        util.set_attr(self, self.env_spec, [
            'name',
            'frame_op',
            'frame_op_len',
            'image_downsize',
            'normalize_state',
            'reward_scale',
            'num_envs',
            'max_t',
            'max_frame',
            'df',
            'window_size',
            'frame_bound',
        ])
        if util.get_lab_mode() == 'eval':  # override if env is for eval
            self.num_envs = ps.get(spec, 'meta.rigorous_eval')
        self.to_render = util.to_render()
        self._infer_frame_attr(spec)
        self._infer_venv_attr()
        self._set_clock()
        self.done = False
        self.total_reward = np.nan

    def _get_spaces(self, u_env):
        '''Helper to set the extra attributes to, and get, observation and action spaces'''
        observation_space = u_env.observation_space
        action_space = u_env.action_space
        set_gym_space_attr(observation_space)
        set_gym_space_attr(action_space)
        return observation_space, action_space

    def _get_observable_dim(self, observation_space):
        '''Get the observable dim for an agent in env'''
        state_dim = observation_space.shape
        if isinstance(observation_space, spaces.MultiDiscrete):
            state_dim = observation_space.nvec.tolist()
        if len(state_dim) == 1:
            state_dim = state_dim[0]
        return {'state': state_dim}

    def _get_action_dim(self, action_space):
        '''Get the action dim for an action_space for agent to use'''
        if isinstance(action_space, spaces.Box):
            assert len(action_space.shape) == 1
            action_dim = action_space.shape[0]
        elif isinstance(action_space, (spaces.Discrete, spaces.MultiBinary)):
            action_dim = action_space.n
        elif isinstance(action_space, spaces.MultiDiscrete):
            action_dim = action_space.nvec.tolist()
        else:
            raise ValueError('action_space not recognized')
        return action_dim

    def _infer_frame_attr(self, spec):
        '''Infer frame attributes'''
        seq_len = ps.get(spec, 'agent.0.net.seq_len')
        if seq_len is not None:  # infer if using RNN
            self.frame_op = 'stack'
            self.frame_op_len = seq_len
        if spec['meta']['distributed'] != False:  # divide max_frame for distributed
            self.max_frame = int(self.max_frame / spec['meta']['max_session'])

    def _infer_venv_attr(self):
        '''Infer vectorized env attributes'''
        self.is_venv = (self.num_envs is not None and self.num_envs > 1)

    def _is_discrete(self, action_space):
        '''Check if an action space is discrete'''
        return util.get_class_name(action_space) != 'Box'

    def _set_clock(self):
        self.clock_speed = 1 * (self.num_envs or 1)  # tick with a multiple of num_envs to properly count frames
        self.clock = Clock(self.max_frame, self.clock_speed)

    def _set_attr_from_u_env(self, u_env):
        '''Set the observation, action dimensions and action type from u_env'''
        self.observation_space, self.action_space = self._get_spaces(u_env)
        self.observable_dim = self._get_observable_dim(self.observation_space)
        self.action_dim = self._get_action_dim(self.action_space)
        self.is_discrete = self._is_discrete(self.action_space)

    def _update_total_reward(self, info):
        '''Extract total_reward from info (set in wrapper) into self.total_reward for single and vec env'''
        if isinstance(info, dict):
            self.total_reward = info['total_reward']
        else:  # vec env tuple of infos
            self.total_reward = np.array([i['total_reward'] for i in info])

    @abstractmethod
    @lab_api
    def reset(self):
        '''Reset method, return state'''
        raise NotImplementedError

    @abstractmethod
    @lab_api
    def step(self, action):
        '''Step method, return state, reward, done, info'''
        raise NotImplementedError

    @abstractmethod
    @lab_api
    def close(self):
        '''Method to close and cleanup env'''
        raise NotImplementedError

import asyncio
from unittest import TestCase
from datetime import datetime, timedelta

from qcodes.instrument.base import Instrument
from qcodes.instrument.mock import MockInstrument
from qcodes.instrument.parameter import Parameter
from qcodes.utils.validators import Numbers, Ints, Strings, MultiType
from qcodes.utils.sync_async import wait_for_async, NoCommandError


class ModelError(Exception):
    pass


class AMockModel(object):
    def __init__(self):
        self._gates = [0.0, 0.0, 0.0]
        self._excitation = 0.1

    def write(self, instrument, parameter, value):
        if instrument == 'gates' and parameter[0] == 'c':
            self._gates[int(parameter[1:])] = float(value)
        elif instrument == 'gates' and parameter == 'rst':
            self._gates = [0.0, 0.0, 0.0]
        elif instrument == 'source' and parameter == 'ampl':
            try:
                self._excitation = float(value)
            except:
                # "Off" as in the MultiType sweep step test
                self._excitation = None
        else:
            raise ModelError('unrecognized write {}, {}, {}'.format(
                instrument, parameter, value))

    def ask(self, instrument, parameter):
        gates = self._gates

        if instrument == 'gates' and parameter[0] == 'c':
            v = gates[int(parameter[1:])]
        elif instrument == 'source' and parameter == 'ampl':
            v = self._excitation
        elif instrument == 'meter' and parameter == 'ampl':
            # here's my super complex model output!
            v = self._excitation * (gates[0] + gates[1]**2 + gates[2]**3)
        elif instrument == 'meter' and parameter[:5] == 'echo ':
            v = float(parameter[5:])
        else:
            raise ModelError('unrecognized ask {}, {}'.format(
                instrument, parameter))

        return '{:.3f}'.format(v)


class TestParamConstructor(TestCase):
    def test_name_s(self):
        p = Parameter('simple')
        self.assertEqual(p.name, 'simple')

        with self.assertRaises(ValueError):
            # you need a name of some sort
            Parameter()

        # or names
        names = ['H1', 'L1']
        p = Parameter(names=names)
        self.assertEqual(p.names, names)
        self.assertFalse(hasattr(p, 'name'))

        # or both, that's OK too.
        names = ['Peter', 'Paul', 'Mary']
        p = Parameter(name='complex', names=names)
        self.assertEqual(p.names, names)
        # TODO: below seems wrong actually - we should let a parameter have
        # a simple name even if it has a names array. But then we need to
        # check everywhere this is used, and make sure everyone who cares
        # about it looks for names first.
        self.assertFalse(hasattr(p, 'name'))

        size = 10
        setpoints = 'we dont check the form of this until later'
        setpoint_names = 'we dont check this either'
        setpoint_labels = 'nor this'
        p = Parameter('makes_array', size=size, setpoints=setpoints,
                      setpoint_names=setpoint_names,
                      setpoint_labels=setpoint_labels)
        self.assertEqual(p.size, size)
        self.assertFalse(hasattr(p, 'sizes'))
        self.assertEqual(p.setpoints, setpoints)
        self.assertEqual(p.setpoint_names, setpoint_names)
        self.assertEqual(p.setpoint_labels, setpoint_labels)

        sizes = [2, 3]
        p = Parameter('makes arrays', sizes=sizes, setpoints=setpoints,
                      setpoint_names=setpoint_names,
                      setpoint_labels=setpoint_labels)
        self.assertEqual(p.sizes, sizes)
        self.assertFalse(hasattr(p, 'size'))
        self.assertEqual(p.setpoints, setpoints)
        self.assertEqual(p.setpoint_names, setpoint_names)
        self.assertEqual(p.setpoint_labels, setpoint_labels)


class TestParameters(TestCase):
    def setUp(self):
        self.model = AMockModel()
        self.read_response = 'I am the walrus!'

        self.gates = MockInstrument('gates', model=self.model, delay=0.001,
                                    use_async=True,
                                    read_response=self.read_response)
        for i in range(3):
            cmdbase = 'c{}'.format(i)
            self.gates.add_parameter('chan{}'.format(i), get_cmd=cmdbase + '?',
                                     set_cmd=cmdbase + ' {:.4f}',
                                     parse_function=float,
                                     vals=Numbers(-10, 10))
            self.gates.add_parameter('chan{}step'.format(i),
                                     get_cmd=cmdbase + '?',
                                     set_cmd=cmdbase + ' {:.4f}',
                                     parse_function=float,
                                     vals=Numbers(-10, 10),
                                     sweep_step=0.1, sweep_delay=0.005)
        self.gates.add_function('reset', call_cmd='rst')

        self.source = MockInstrument('source', model=self.model, delay=0.001)
        self.source.add_parameter('amplitude', get_cmd='ampl?',
                                  set_cmd='ampl {:.4f}', parse_function=float,
                                  vals=Numbers(0, 1),
                                  sweep_step=0.2, sweep_delay=0.005)

        self.meter = MockInstrument('meter', model=self.model, delay=0.001,
                                    read_response=self.read_response)
        self.meter.add_parameter('amplitude', get_cmd='ampl?',
                                 parse_function=float)
        self.meter.add_function('echo', call_cmd='echo {:.2f}?',
                                parameters=[Numbers(0, 1000)],
                                parse_function=float)

        self.init_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def check_ts(self, ts_str):
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.assertTrue(self.init_ts <= ts_str <= now)

    def test_mock_instrument(self):
        gates, source, meter = self.gates, self.source, self.meter

        # initial state
        # short form of getter
        self.assertEqual(meter.get('amplitude'), 0)
        # shortcut to the parameter, longer form of get
        self.assertEqual(meter['amplitude'].get(), 0)
        # explicit long form of getter
        self.assertEqual(meter.parameters['amplitude'].get(), 0)
        # both should produce the same history entry
        self.assertEqual(len(meter.history), 3)
        self.assertEqual(meter.history[0][1:], ('ask', 'ampl'))
        self.assertEqual(meter.history[0][1:], ('ask', 'ampl'))

        # errors trying to set (or validate) invalid param values
        # put here so we ensure that these errors don't make it to
        # the history (ie they don't result in hardware commands)
        with self.assertRaises(ValueError):
            gates.set('chan1', '1')
        with self.assertRaises(ValueError):
            gates.parameters['chan1'].validate('1')

        # change one param at a time
        gates.set('chan0', 0.5)
        self.assertEqual(gates.get('chan0'), 0.5)
        self.assertEqual(meter.get('amplitude'), 0.05)

        gates.set('chan1', 2)
        self.assertEqual(gates.get('chan1'), 2)
        self.assertEqual(meter.get('amplitude'), 0.45)

        gates.set('chan2', -3.2)
        self.assertEqual(gates.get('chan2'), -3.2)
        self.assertEqual(meter.get('amplitude'), -2.827)

        source.set('amplitude', 0.6)
        self.assertEqual(source.get('amplitude'), 0.6)
        self.assertEqual(meter.get('amplitude'), -16.961)

        # check just the size and timestamps of histories
        for entry in gates.history + source.history + meter.history:
            self.check_ts(entry[0])
        self.assertEqual(len(gates.history), 6)
        self.assertEqual(len(meter.history), 7)
        self.assertEqual(len(source.history), 5)

        # plus enough setters to check the parameter sweep
        # first source has to get the starting value
        self.assertEqual(source.history[0][1:], ('ask', 'ampl'))
        # then it writes each
        self.assertEqual(source.history[1][1:], ('write', 'ampl', '0.3000'))
        self.assertEqual(source.history[2][1:], ('write', 'ampl', '0.5000'))
        self.assertEqual(source.history[3][1:], ('write', 'ampl', '0.6000'))

        # test sync/async - so far all calls have been sync, even though gates
        # was defined as async. Mock some async calls to test other conversions
        wait_for_async(source.set_async, 'amplitude', 0.8)
        self.assertEqual(wait_for_async(source.get_async, 'amplitude'), 0.8)
        wait_for_async(gates.set_async, 'chan1', -2)
        self.assertEqual(wait_for_async(gates.get_async, 'chan1'), -2)

        # test functions
        self.assertEqual(meter.call('echo', 1.2345), 1.23)  # model returns .2f
        # too many ways to do this...
        self.assertEqual(meter.echo.call(1.2345), 1.23)
        self.assertEqual(meter.echo(1.2345), 1.23)
        self.assertEqual(meter['echo'].call(1.2345), 1.23)
        self.assertEqual(meter['echo'](1.2345), 1.23)
        with self.assertRaises(TypeError):
            meter.call('echo', 1, 2)
        with self.assertRaises(ValueError):
            meter.call('echo', '1')

        # validating before actually trying to call
        with self.assertRaises(TypeError):
            meter.functions['echo'].validate(1, 2)
        with self.assertRaises(ValueError):
            meter.functions['echo'].validate('1')
        gates.call('reset')
        self.assertEqual(gates.get('chan0'), 0)

        # and async functions
        self.assertEqual(wait_for_async(meter.call_async, 'echo', 4.567), 4.57)
        gates.set('chan0', 1)
        self.assertEqual(gates.get('chan0'), 1)
        wait_for_async(gates.call_async, 'reset')
        self.assertEqual(gates.get('chan0'), 0)

    def test_mock_async_set_sweep(self):
        gates = self.gates
        wait_for_async(gates.set_async, 'chan0step', 0.5)
        self.assertEqual(len(gates.history), 6)
        self.assertEqual(
            [float(h[3]) for h in gates.history if h[1] == 'write'],
            [0.1, 0.2, 0.3, 0.4, 0.5])

    def test_mock_instrument_errors(self):
        gates, meter = self.gates, self.meter
        with self.assertRaises(ValueError):
            gates.ask('no question')
        with self.assertRaises(ValueError):
            gates.ask('question?yes but more after')

        with self.assertRaises(ModelError):
            gates.write('ampl 1')
        with self.assertRaises(ModelError):
            gates.ask('ampl?')

        with self.assertRaises(TypeError):
            MockInstrument('', delay='forever')
        with self.assertRaises(TypeError):
            MockInstrument('', delay=-1)

        with self.assertRaises(AttributeError):
            MockInstrument('', model=None)

        with self.assertRaises(KeyError):
            gates.add_parameter('chan0', get_cmd='boo')
        with self.assertRaises(KeyError):
            gates.add_function('reset', call_cmd='hoo')

        with self.assertRaises(NotImplementedError):
            meter.set('amplitude', 0.5)
        meter.add_parameter('gain', set_cmd='gain {:.3f}')
        with self.assertRaises(NotImplementedError):
            meter.get('gain')

        with self.assertRaises(TypeError):
            gates.add_parameter('fugacity', set_cmd='f {:.4f}', vals=[1, 2, 3])

    def test_sweep_steps_edge_case(self):
        # MultiType with sweeping is weird - not sure why one would do this,
        # but we should handle it
        source = self.source
        source.add_parameter('amplitude2', get_cmd='ampl?',
                             set_cmd='ampl {}', parse_function=float,
                             vals=MultiType(Numbers(0, 1), Strings()),
                             sweep_step=0.2, sweep_delay=0.005)
        self.assertEqual(len(source.history), 0)
        source.set('amplitude2', 'Off')
        self.assertEqual(len(source.history), 2)  # get then set
        source.set('amplitude2', 0.2)
        self.assertEqual(len(source.history), 3)  # single set
        source.set('amplitude2', 0.8)  # num -> num is the only real sweep
        self.assertEqual(len(source.history), 6)  # 3-step sweep
        source.set('amplitude2', 'Off')
        self.assertEqual(len(source.history), 7)  # single set

    def test_set_sweep_errors(self):
        gates = self.gates

        # for reference, some add_parameter's that should work
        gates.add_parameter('t0', set_cmd='{}', vals=Numbers(),
                            sweep_step=0.1, sweep_delay=0.01)
        gates.add_parameter('t2', set_cmd='{}', vals=Ints(),
                            sweep_step=1, sweep_delay=0.01,
                            max_val_age=0)

        with self.assertRaises(TypeError):
            # can't sweep non-numerics
            gates.add_parameter('t1', set_cmd='{}', vals=Strings(),
                                sweep_step=1, sweep_delay=0.01)
        with self.assertRaises(TypeError):
            # need a numeric step too
            gates.add_parameter('t1', set_cmd='{}', vals=Numbers(),
                                sweep_step='a skosh', sweep_delay=0.01)
        with self.assertRaises(TypeError):
            # Ints requires and int step
            gates.add_parameter('t1', set_cmd='{}', vals=Ints(),
                                sweep_step=0.1, sweep_delay=0.01)
        with self.assertRaises(ValueError):
            # need a positive step
            gates.add_parameter('t1', set_cmd='{}', vals=Numbers(),
                                sweep_step=0, sweep_delay=0.01)
        with self.assertRaises(ValueError):
            # need a positive step
            gates.add_parameter('t1', set_cmd='{}', vals=Numbers(),
                                sweep_step=-0.1, sweep_delay=0.01)
        with self.assertRaises(TypeError):
            # need a numeric delay
            gates.add_parameter('t1', set_cmd='{}', vals=Numbers(),
                                sweep_step=0.1, sweep_delay='a tad')
        with self.assertRaises(ValueError):
            # need a positive delay
            gates.add_parameter('t1', set_cmd='{}', vals=Numbers(),
                                sweep_step=0.1, sweep_delay=-0.01)
        with self.assertRaises(ValueError):
            # need a positive delay
            gates.add_parameter('t1', set_cmd='{}', vals=Numbers(),
                                sweep_step=0.1, sweep_delay=0)
        with self.assertRaises(TypeError):
            # need a numeric max_val_age
            gates.add_parameter('t1', set_cmd='{}', vals=Numbers(),
                                sweep_step=0.1, sweep_delay=0.01,
                                max_val_age='an hour')
        with self.assertRaises(ValueError):
            # need a non-negative max_val_age
            gates.add_parameter('t1', set_cmd='{}', vals=Numbers(),
                                sweep_step=0.1, sweep_delay=0.01,
                                max_val_age=-1)

    def test_snapshot(self):
        self.assertEqual(self.meter.snapshot(), {
            'parameters': {'amplitude': {}},
            'functions': {'echo': {}}
        })

        ampsnap = self.meter.snapshot(update=True)['parameters']['amplitude']
        amp = self.meter.get('amplitude')
        self.assertEqual(ampsnap['value'], amp)
        amp_ts = datetime.strptime(ampsnap['ts'], '%Y-%m-%d %H:%M:%S')
        self.assertLessEqual(amp_ts, datetime.now())
        self.assertGreater(amp_ts, datetime.now() - timedelta(seconds=1.1))

    def test_mock_read(self):
        gates, meter = self.gates, self.meter
        self.assertEqual(meter.read(), self.read_response)
        self.assertEqual(wait_for_async(meter.read_async),
                         self.read_response)
        self.assertEqual(gates.read(), self.read_response)
        self.assertEqual(wait_for_async(gates.read_async),
                         self.read_response)

    def test_base_instrument_errors(self):
        b = Instrument('silent')

        with self.assertRaises(NotImplementedError):
            b.read()
        with self.assertRaises(NotImplementedError):
            b.write('hello!')
        with self.assertRaises(NotImplementedError):
            b.ask('how are you?')

        with self.assertRaises(NotImplementedError):
            wait_for_async(b.read_async)
        with self.assertRaises(NotImplementedError):
            wait_for_async(b.write_async, 'goodbye')
        with self.assertRaises(NotImplementedError):
            wait_for_async(b.ask_async, 'are we having fun yet?')

        with self.assertRaises(TypeError):
            b.add_function('skip', call_cmd='skip {}',
                           parameters=['not a validator'])
        with self.assertRaises(NoCommandError):
            b.add_function('jump')
        with self.assertRaises(NoCommandError):
            b.add_parameter('height')

    def test_sweep_values_errors(self):
        gates, source, meter = self.gates, self.source, self.meter
        c0 = gates.parameters['chan0']
        source_amp = source.parameters['amplitude']
        meter_amp = meter.parameters['amplitude']

        # only complete 3-part slices are valid
        with self.assertRaises(TypeError):
            c0[1:2]  # For Int params this could be defined as step=1
        with self.assertRaises(TypeError):
            c0[:2:3]
        with self.assertRaises(TypeError):
            c0[1::3]
        with self.assertRaises(TypeError):
            c0[:]  # For Enum params we *could* define this one too...

        # fails if the parameter has no setter
        # with self.assertRaises(AttributeError):
        meter_amp[0]

        # validates every step value against the parameter's Validator
        with self.assertRaises(ValueError):
            c0[5:15:1]
        with self.assertRaises(ValueError):
            c0[5.0:15.0:1.0]
        with self.assertRaises(ValueError):
            c0[-12]
        with self.assertRaises(ValueError):
            c0[-5, 12, 5]
        with self.assertRaises(ValueError):
            c0[-5, 12:8:1, 5]

        # cannot combine SweepValues for different parameters
        with self.assertRaises(TypeError):
            c0[0.1] + source_amp[0.2]

        # improper use of extend
        with self.assertRaises(TypeError):
            c0[0.1].extend(5)

        # SweepValue object has no getter, even if the parameter does
        with self.assertRaises(AttributeError):
            c0[0.1].get
        with self.assertRaises(AttributeError):
            c0[0.1].get_async

    def test_sweep_values_valid(self):
        gates = self.gates
        c0 = gates.parameters['chan0']
        c1_noasync = gates.parameters['chan1']
        del c1_noasync.set_async
        with self.assertRaises(AttributeError):
            c1_noasync.set_async
        c2_nosync = gates.parameters['chan2']
        del c2_nosync.set
        with self.assertRaises(AttributeError):
            c2_nosync.set

        c0_sv = c0[1]
        c1_sv = c1_noasync[1]
        c2_sv = c2_nosync[1]
        # setters get mapped
        self.assertEqual(c0_sv.set, c0.set)
        self.assertEqual(c0_sv.set_async, c0.set_async)
        self.assertEqual(c1_sv.set, c1_noasync.set)
        self.assertTrue(asyncio.iscoroutinefunction(c1_sv.set_async))
        self.assertEqual(c2_sv.set_async, c2_nosync.set_async)
        self.assertTrue(callable(c2_sv.set))
        # normal sequence operations access values
        self.assertEqual(list(c0_sv), [1])
        self.assertEqual(c0_sv[0], 1)
        self.assertTrue(1 in c0_sv)
        self.assertFalse(2 in c0_sv)

        # in-place and copying addition
        c0_sv += c0[1.5:1.8:0.1]
        c0_sv2 = c0_sv + c0[2]
        self.assertEqual(list(c0_sv), [1, 1.5, 1.6, 1.7])
        self.assertEqual(list(c0_sv2), [1, 1.5, 1.6, 1.7, 2])

        # append and extend
        c0_sv3 = c0[2]
        # append only works with straight values
        c0_sv3.append(2.1)
        # extend can use another SweepValue, (even if it only has one value)
        c0_sv3.extend(c0[2.2])
        # extend can also take a sequence
        c0_sv3.extend([2.3])
        # as can addition
        c0_sv3 += [2.4]
        c0_sv4 = c0_sv3 + [2.5, 2.6]
        self.assertEqual(list(c0_sv3), [2, 2.1, 2.2, 2.3, 2.4])
        self.assertEqual(list(c0_sv4), [2, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6])

        # len
        self.assertEqual(len(c0_sv3), 5)

        # in-place and copying reverse
        c0_sv.reverse()
        c0_sv5 = reversed(c0_sv)
        self.assertEqual(list(c0_sv), [1.7, 1.6, 1.5, 1])
        self.assertEqual(list(c0_sv5), [1, 1.5, 1.6, 1.7])

        # multi-key init, where first key is itself a list
        c0_sv6 = c0[[1, 3], 4]
        # copying
        c0_sv7 = c0_sv6.copy()
        self.assertEqual(list(c0_sv6), [1, 3, 4])
        self.assertEqual(list(c0_sv7), [1, 3, 4])
        self.assertFalse(c0_sv6 is c0_sv7)

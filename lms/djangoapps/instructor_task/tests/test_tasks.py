"""
Unit tests for LMS instructor-initiated background tasks, 

Runs tasks on answers to course problems to validate that code
paths actually work.

"""
import logging
import json
from uuid import uuid4

from mock import Mock, patch

from celery.states import SUCCESS, FAILURE

from xmodule.modulestore.exceptions import ItemNotFoundError

from courseware.model_data import StudentModule
from courseware.tests.factories import StudentModuleFactory
from student.tests.factories import UserFactory

from instructor_task.models import InstructorTask
from instructor_task.tests.test_base import InstructorTaskModuleTestCase, TEST_COURSE_ORG, TEST_COURSE_NUMBER
from instructor_task.tests.factories import InstructorTaskFactory
from instructor_task.tasks import rescore_problem, reset_problem_attempts, delete_problem_state
from instructor_task.tasks_helper import UpdateProblemModuleStateError


log = logging.getLogger(__name__)
PROBLEM_URL_NAME = "test_urlname"


class TestTaskFailure(Exception):
    pass


class TestInstructorTasks(InstructorTaskModuleTestCase):
    def setUp(self):
        super(InstructorTaskModuleTestCase, self).setUp()
        self.initialize_course()
        self.instructor = self.create_instructor('instructor')
        self.problem_url = InstructorTaskModuleTestCase.problem_location(PROBLEM_URL_NAME)

    def _create_input_entry(self, student_ident=None):
        """Creates a InstructorTask entry for testing."""
        task_id = str(uuid4())
        task_input = {'problem_url': self.problem_url}
        if student_ident is not None:
            task_input['student'] = student_ident

        instructor_task = InstructorTaskFactory.create(course_id=self.course.id,
                                                       requester=self.instructor,
                                                       task_input=json.dumps(task_input),
                                                       task_key='dummy value',
                                                       task_id=task_id)
        return instructor_task

    def _get_xmodule_instance_args(self):
        """
        Calculate dummy values for parameters needed for instantiating xmodule instances.
        """
        return {'xqueue_callback_url_prefix': 'dummy_value',
                'request_info': {},
                }

    def _run_task_with_mock_celery(self, task_class, entry_id, task_id, expected_failure_message=None):
        self.current_task = Mock()
        self.current_task.request = Mock()
        self.current_task.request.id = task_id
        self.current_task.update_state = Mock()
        if expected_failure_message is not None:
            self.current_task.update_state.side_effect = TestTaskFailure(expected_failure_message)
        with patch('instructor_task.tasks_helper._get_current_task') as mock_get_task:
            mock_get_task.return_value = self.current_task
            return task_class(entry_id, self._get_xmodule_instance_args())

    def test_missing_current_task(self):
        # run without (mock) Celery running
        task_entry = self._create_input_entry()
        with self.assertRaises(UpdateProblemModuleStateError):
            reset_problem_attempts(task_entry.id, self._get_xmodule_instance_args())

    def test_undefined_problem(self):
        # run with celery, but no problem defined
        task_entry = self._create_input_entry()
        with self.assertRaises(ItemNotFoundError):
            self._run_task_with_mock_celery(reset_problem_attempts, task_entry.id, task_entry.task_id)

    def _assert_return_matches_entry(self, returned, entry_id):
        entry = InstructorTask.objects.get(id=entry_id)
        self.assertEquals(returned, json.loads(entry.task_output))

    def _test_run_with_task(self, task_class, action_name, expected_num_updated):
        # run with some StudentModules for the problem
        task_entry = self._create_input_entry()
        status = self._run_task_with_mock_celery(task_class, task_entry.id, task_entry.task_id)
        # check return value
        self.assertEquals(status.get('attempted'), expected_num_updated)
        self.assertEquals(status.get('updated'), expected_num_updated)
        self.assertEquals(status.get('total'), expected_num_updated)
        self.assertEquals(status.get('action_name'), action_name)
        self.assertTrue('duration_ms' in status)
        # compare with entry in table:
        entry = InstructorTask.objects.get(id=task_entry.id)
        self.assertEquals(json.loads(entry.task_output), status)
        self.assertEquals(entry.task_state, SUCCESS)

    def _test_run_with_no_state(self, task_class, action_name):
        # run with no StudentModules for the problem
        self.define_option_problem(PROBLEM_URL_NAME)
        self._test_run_with_task(task_class, action_name, 0)

    def test_rescore_with_no_state(self):
        self._test_run_with_no_state(rescore_problem, 'rescored')

    def test_reset_with_no_state(self):
        self._test_run_with_no_state(reset_problem_attempts, 'reset')

    def test_delete_with_no_state(self):
        self._test_run_with_no_state(delete_problem_state, 'deleted')

    def _create_some_students(self, num_students, state=None):
        self.define_option_problem(PROBLEM_URL_NAME)
        students = [
            UserFactory.create(username='robot%d' % i, email='robot+test+%d@edx.org' % i)
            for i in xrange(num_students)
        ]
        for student in students:
            StudentModuleFactory.create(course_id=self.course.id,
                                        module_state_key=self.problem_url,
                                        student=student,
                                        state=state)
        return students

    def test_reset_with_some_state(self):
        initial_attempts = 3
        input_state = json.dumps({'attempts': initial_attempts})
        num_students = 10
        students = self._create_some_students(num_students, input_state)
        # check that entries were set correctly
        for student in students:
            module = StudentModule.objects.get(course_id=self.course.id,
                                               student=student,
                                               module_state_key=self.problem_url)
            state = json.loads(module.state)
            self.assertEquals(state['attempts'], initial_attempts)
        # run the task
        self._test_run_with_task(reset_problem_attempts, 'reset', num_students)
        # check that entries were reset
        for student in students:
            module = StudentModule.objects.get(course_id=self.course.id,
                                               student=student,
                                               module_state_key=self.problem_url)
            state = json.loads(module.state)
            self.assertEquals(state['attempts'], 0)

    def test_delete_with_some_state(self):
        # This will create StudentModule entries -- we don't have to worry about
        # the state inside them.
        num_students = 10
        students = self._create_some_students(num_students)
        # check that entries were created correctly
        for student in students:
            StudentModule.objects.get(course_id=self.course.id,
                                               student=student,
                                               module_state_key=self.problem_url)
        self._test_run_with_task(delete_problem_state, 'deleted', num_students)
        # confirm that no state can be found anymore:
        for student in students:
            with self.assertRaises(StudentModule.DoesNotExist):
                StudentModule.objects.get(course_id=self.course.id,
                                          student=student,
                                          module_state_key=self.problem_url)

    def _test_reset_with_student(self, use_email):
        # run with some StudentModules for the problem
        num_students = 10
        initial_attempts = 3
        input_state = json.dumps({'attempts': initial_attempts})
        students = self._create_some_students(num_students, input_state)
        # check that entries were set correctly
        for student in students:
            module = StudentModule.objects.get(course_id=self.course.id,
                                               student=student,
                                               module_state_key=self.problem_url)
            state = json.loads(module.state)
            self.assertEquals(state['attempts'], initial_attempts)

        if use_email:
            student_ident = students[3].email
        else:
            student_ident = students[3].username
        task_entry = self._create_input_entry(student_ident)

        status = self._run_task_with_mock_celery(reset_problem_attempts, task_entry.id, task_entry.task_id)
        # check return value
        self.assertEquals(status.get('attempted'), 1)
        self.assertEquals(status.get('updated'), 1)
        self.assertEquals(status.get('total'), 1)
        self.assertEquals(status.get('action_name'), 'reset')
        self.assertTrue('duration_ms' in status)
        # compare with entry in table:
        entry = InstructorTask.objects.get(id=task_entry.id)
        self.assertEquals(json.loads(entry.task_output), status)
        self.assertEquals(entry.task_state, SUCCESS)
        # check that the correct entry was reset
        for index, student in enumerate(students):
            module = StudentModule.objects.get(course_id=self.course.id,
                                               student=student,
                                               module_state_key=self.problem_url)
            state = json.loads(module.state)
            if index == 3:
                self.assertEquals(state['attempts'], 0)
            else:
                self.assertEquals(state['attempts'], initial_attempts)

    def test_reset_with_student_username(self):
        self._test_reset_with_student(False)

    def test_reset_with_student_email(self):
        self._test_reset_with_student(True)

    def _test_run_with_failure(self, task_class, expected_message):
        # run with no StudentModules for the problem,
        # because we will fail before entering the loop.
        task_entry = self._create_input_entry()
        self.define_option_problem(PROBLEM_URL_NAME)
        try:
            self._run_task_with_mock_celery(task_class, task_entry.id, task_entry.task_id, expected_message)
        except TestTaskFailure:
            pass
        # compare with entry in table:
        entry = InstructorTask.objects.get(id=task_entry.id)
        self.assertEquals(entry.task_state, FAILURE)
        output = json.loads(entry.task_output)
        self.assertEquals(output['exception'], 'TestTaskFailure')
        self.assertEquals(output['message'], expected_message)

    def test_rescore_with_failure(self):
        self._test_run_with_failure(rescore_problem, 'We expected this to fail')

    def test_reset_with_failure(self):
        self._test_run_with_failure(reset_problem_attempts, 'We expected this to fail')

    def test_delete_with_failure(self):
        self._test_run_with_failure(delete_problem_state, 'We expected this to fail')

    def _test_run_with_long_error_msg(self, task_class):
        # run with an error message that is so long it will require
        # truncation (as well as the jettisoning of the traceback).
        task_entry = self._create_input_entry()
        self.define_option_problem(PROBLEM_URL_NAME)
        expected_message = "x" * 1500
        try:
            self._run_task_with_mock_celery(task_class, task_entry.id, task_entry.task_id, expected_message)
        except TestTaskFailure:
            pass
        # compare with entry in table:
        entry = InstructorTask.objects.get(id=task_entry.id)
        self.assertEquals(entry.task_state, FAILURE)
        self.assertGreater(1023, len(entry.task_output))
        output = json.loads(entry.task_output)
        self.assertEquals(output['exception'], 'TestTaskFailure')
        self.assertEquals(output['message'], expected_message[:len(output['message']) - 3] + "...")
        self.assertTrue('traceback' not in output)

    def test_rescore_with_long_error_msg(self):
        self._test_run_with_long_error_msg(rescore_problem)

    def test_reset_with_long_error_msg(self):
        self._test_run_with_long_error_msg(reset_problem_attempts)

    def test_delete_with_long_error_msg(self):
        self._test_run_with_long_error_msg(delete_problem_state)

    def _test_run_with_short_error_msg(self, task_class):
        # run with an error message that is short enough to fit
        # in the output, but long enough that the traceback won't.
        # Confirm that the traceback is truncated.
        task_entry = self._create_input_entry()
        self.define_option_problem(PROBLEM_URL_NAME)
        expected_message = "x" * 900
        try:
            self._run_task_with_mock_celery(task_class, task_entry.id, task_entry.task_id, expected_message)
        except TestTaskFailure:
            pass
        # compare with entry in table:
        entry = InstructorTask.objects.get(id=task_entry.id)
        self.assertEquals(entry.task_state, FAILURE)
        self.assertGreater(1023, len(entry.task_output))
        output = json.loads(entry.task_output)
        self.assertEquals(output['exception'], 'TestTaskFailure')
        self.assertEquals(output['message'], expected_message)
        self.assertEquals(output['traceback'][-3:], "...")

    def test_rescore_with_short_error_msg(self):
        self._test_run_with_short_error_msg(rescore_problem)

    def test_reset_with_short_error_msg(self):
        self._test_run_with_short_error_msg(reset_problem_attempts)

    def test_delete_with_short_error_msg(self):
        self._test_run_with_short_error_msg(delete_problem_state)

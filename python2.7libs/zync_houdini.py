import glob
import hou
import os
import re
import sys
import webbrowser

if os.environ.get('ZYNC_API_DIR'):
  API_DIR = os.environ.get('ZYNC_API_DIR')
else:
  config_path = os.path.join(os.path.dirname(__file__), 'config_houdini.py')
  if not os.path.exists(config_path):
    raise Exception('Could not locate config_houdini.py, please create. '
                    'You can use config_houdini.py.example as an example.')
  import config_houdini
  API_DIR = config_houdini.API_DIR

if not API_DIR:
  raise Exception('config_houdini.py must define a value for API_DIR')

if not API_DIR in sys.path:
  sys.path.append(API_DIR)


import zync
import file_select_dialog


__version__ = '1.5.3'


class JobCreationError(Exception):
  """Exception to handle problems while submiting job"""
  pass


class ParameterError(JobCreationError):
  """Exception to handle parameter validation error."""
  pass


class AbortedByUser(JobCreationError):
  """Exception to handle user action of canceling the submission."""
  pass


class ZyncConnection(object):
  # Implementation of singleton
  instance = None

  def __init__(self):
    """Implementation of singleton."""
    if not ZyncConnection.instance:
      ZyncConnection.instance = ZyncConnection.ZyncConnectionInner()

  def __getattr__(self, name):
    """Implementation of singleton."""
    return getattr(self.instance, name)

  class ZyncConnectionInner(object):
    """Wrapper around zync.Zync class."""
    def __init__(self):
      self.instance_types = {}
      self.project_list = None
      self.zync_conn = None

    def login(self):
      """Performs user login."""
      try:
        if self.zync_conn is None:
          self.zync_conn = zync.Zync(application='houdini')

        if not self.zync_conn.has_user_login():
          self.zync_conn.login_with_google()
      except zync.ZyncConnectionError as ce:
        hou.ui.displayMessage(text=str(ce))

    def logout(self):
      """Performs user logout.
      """
      if self.zync_conn:
        self.zync_conn.logout()

    def get_site_name(self):
      """Returns name of the Zync site.

      Returns:
        str
      """
      return zync.ZYNC_URL

    def get_user_email(self):
      """Returns user's email.

      Returns:
        str
      """
      if self.is_logged_in():
        return self.zync_conn.email
      return ''

    def is_logged_in(self):
      """Tells if user is logged in.

      Returns:
        bool
      """
      return self.zync_conn is not None and self.zync_conn.has_user_login()

    def is_v2(self):
      """Tells if the backend is v2.

      Returns:
        bool
      """
      if self.zync_conn:
        return 'ZYNC_BACKEND_VERSION' in self.zync_conn.CONFIG and self.zync_conn.CONFIG['ZYNC_BACKEND_VERSION'] == 2
      return False

    def get_project_list(self, force_fetch=False):
      """Lists available projects.

      Args:
        force_fetch: bool, Force updating list via API call.
      Returns:
        [str], List of available projects.
      """
      if not self.zync_conn:
        return []
      if force_fetch or (not self.project_list):
        self.project_list = self.zync_conn.get_project_list()
      return self.project_list

    def get_machine_types(self, renderer):
      """Returns data on available machine types.

      Args:
        renderer: str, Name of the renderer.

      Returns:
        [dict()], List of objects representing machine types.
      """
      if not self.zync_conn:
        return {}

      if renderer not in self.instance_types:
        self.instance_types[renderer] = self.zync_conn.get_instance_types(
            renderer=renderer, usage_tag='houdini_%s' % renderer)

      return self.instance_types[renderer]

    def get_unit_price_of_machine(self, renderer, machine_type):
      """Calculates unit cost of computation.

      Args:
        renderer: str, Render software name
        machine_type: str, Type of used machine.

      Returns:
        float, Cost of running single machine for one hour.
      """
      machine_data = self.get_machine_types(renderer).get(machine_type, {})
      return machine_data.get('cost', None)

    def submit_job(self, node):
      """Starts process of sending job to Zync

      Args:
        node: hou.Node, Node containing data to be sent.
      """
      if not self.zync_conn:
        hou.ui.displayMessage(text='Problem with connection, Try to log in again')
        return

      if not self.zync_conn.has_user_login():
        hou.ui.displayMessage(text='Please log in before submission')
        return

      try:
        job_data = ZyncHoudiniJob(node)
      except JobCreationError as e:
        hou.ui.displayMessage(text=str(e))
        return

      if not self.check_eulas():
        return

      params = job_data.params_to_send()
      if not self.maybe_show_pvm_warning(params):
        return

      try:
        self.zync_conn.submit_job('houdini', job_data.scene_path(), params=params)
        hou.ui.displayMessage(text='Job submitted to Zync.')
        post_submit_job(node)
      except AbortedByUser:
        pass
      except zync.ZyncPreflightError as e:
        hou.ui.displayMessage(title='Preflight Check Failed', text=str(e),
            severity=hou.severityType.Error)
      except zync.ZyncError as e:
        hou.ui.displayMessage(title='Submission Error',
            text='Error submitting job: %s' % (str(e),),
            severity=hou.severityType.Error)

    def check_eulas(self):
      """Tells if user has accepted EULA. If not, it helps user to find place to
         do so.

      Returns:
        bool, User has accepted EULA?
      """
      applicable_eula_types = ['zync', 'cloud', 'licensor', 'houdini-beta']
      to_accept = [eula for eula in self.zync_conn.get_eulas()
                   if eula.get('eula_kind').lower() in applicable_eula_types]
      # Blank accepted_by field indicates agreement is not yet accepted.
      not_accepted = [eula for eula in to_accept if not eula.get('accepted_by')]
      if not_accepted:
        eula_url = '%s/account#legal' % self.zync_conn.url
        message = (
            'In order to launch Houdini jobs you must accept the EULA. It looks '
            'like you haven\'t accepted this yet.\n\nA browser window will open '
            'so you can do this, then you\'ll be able to submit your job.\n\n'
            'URL: ' + eula_url)
        if hou.ui.displayMessage(message, buttons=("OK", "Cancel"),
                                 close_choice=1) == 0:
          webbrowser.open(eula_url)
        return False

      return True


    def maybe_show_pvm_warning(self, params):
      version_major, _, _ = hou.applicationVersionString().split('.')
      if int(version_major) < 16:
        # Prior to V 16 Houdini didn't have PySide
        return True

      if not 'PREEMPTIBLE' in params['instance_type']:
        return True

      import pvm_consent_dialog
      from settings import Settings

      consent_dialog = pvm_consent_dialog.PvmConsentDialog()

      return Settings.get().get_pvm_ack() or consent_dialog.prompt()


class ZyncHoudiniJob(object):
  """Wrapper around sending job to Zync"""
  def __init__(self, node):
    """Constructor.

    Args:
      node: hou.Node. Node containing data needed to create a submission.
    """
    self.node = node

  @staticmethod
  def scene_path():
    """Location of HIP file.

    Returns: str, Location of HIP file.
    """
    return hou.hipFile.path()

  @staticmethod
  def get_dependencies(frame_begin, frame_end, step):
    """Finds all file dependencies in the project for specific range of frames.

    Args:
      frame_begin: First frame (inclusive) of range to render.
      frame_end: Last frame (inclusive) of range to render.
      step: Frame range step.

    Returns:
      set of str, List of detected dependencies.
    """
    refs = hou.fileReferences()
    frames = xrange(int(frame_begin), int(frame_end) + 1, int(step))

    result = set()

    wildcards = ['<udim>', '$SF']
    wildcard_regex = [re.compile(re.escape(wc), re.IGNORECASE)
                      for wc in wildcards]

    for frame in frames:
      for parm, _ in refs:
        if parm:
          file_path = parm.evalAtFrame(frame)
          if file_path in result:
            continue
          for wildcard in wildcard_regex:
            file_path = wildcard.sub('*', file_path)
          for file in glob.glob(file_path):
            try:
              if hou.findFile(file) and file not in result:
                result.add(file)
            except hou.OperationFailed:
              pass
    return result

  def params_to_send(self):
    """Collects and verifies parameters.

    Raises:
      ParameterError: One of the parameters is invalid.

    Returns:
      {str, object}, Verified parameters to send.
    """
    params = self.get_raw_params()
    self.check_params(params)
    return params

  @staticmethod
  def check_params(params):
    """Validate submission parameters

    Args:
      params: {str, object}, Parameters to be validated.

    Raises:
      ParameterError: One of the parameters is invalid.

    Returns:
      {str, object}, Valid paramters
    """
    if not params.get('proj_name', ''):
      raise ParameterError('Project name cannot be empty')
    if not params.get('instance_type', ''):
      raise ParameterError('Machine type cannot be empty')
    _, scene_ext = os.path.splitext(ZyncHoudiniJob.scene_path())

    # Standalone rendering is not available for not-commercial or
    # limited-commercial users.
    commercial_license = (
        hou.licenseCategory() == hou.licenseCategoryType.Commercial)
    if not commercial_license:
      params['use_standalone'] = 0


  @staticmethod
  def fetch_data_from_mantra_node(input_node):
    """Collects data form Mantra input node.

    Assumes that input node is Mantra node

    Args:
      input_node: hou.Node, Mantra node.

    Returns:
      {str, object}, Submission parameters.
    """
    output_picture = input_node.parm('vm_picture').unexpandedString()

    result = dict(
      output_filename=os.path.basename(output_picture),
      renderer='mantra',
      renderer_version=hou.applicationVersion(),
      render_current_frame=False
    )

    if input_node.parm('trange').evalAsString() == 'off':
      current_frame = hou.frame()
      result['frame_begin'] = current_frame
      result['frame_end'] = current_frame
      result['step'] = 1
      # Resolution limits only apply to non- and limited-commercial, so "Render Current Frame"
      # isn't needed otherwise.
      result['render_current_frame'] = (hou.licenseCategory() != hou.licenseCategoryType.Commercial)
    else:
      result['frame_begin'] = input_node.parm('f1').eval()
      result['frame_end'] = input_node.parm('f2').eval()
      result['step'] = input_node.parm('f3').eval()

    return result

  @staticmethod
  def fetch_data_from_arnold_node(input_node):
    """Collects data form Arnold input node.

    Assumes that input node is Arnold node

    Args:
      input_node: hou.Node, Arnold node.

    Returns:
      {str, object}, Submission parameters.
    """
    output_picture = input_node.parm('ar_picture').unexpandedString()

    import htoa
    arnold_version = htoa.__version__

    result = dict(
      output_filename=os.path.basename(output_picture),
      renderer='arnold',
      renderer_version=arnold_version,
      render_current_frame=False
    )

    if input_node.parm('trange').evalAsString() == 'off':
      current_frame = hou.frame()
      result['frame_begin'] = current_frame
      result['frame_end'] = current_frame
      result['step'] = 1
      # Resolution limits only apply to non- and limited-commercial, so "Render Current Frame"
      # isn't needed otherwise.
      result['render_current_frame'] = (hou.licenseCategory() != hou.licenseCategoryType.Commercial)
    else:
      result['frame_begin'] = input_node.parm('f1').eval()
      result['frame_end'] = input_node.parm('f2').eval()
      result['step'] = input_node.parm('f3').eval()

    return result

  @staticmethod
  def fetch_data_from_redshift_node(input_node):
    """Collects data form Redshift input node.

    Assumes that input node is Redshift node

    Args:
      input_node: hou.Node, Redshift node.

    Returns:
      {str, object}, Submission parameters.
    """
    output_picture = input_node.parm('RS_outputFileNamePrefix').unexpandedString()

    redshift_version = hou.hscript("Redshift_version")[0].strip()

    result = dict(
      output_filename=os.path.basename(output_picture),
      renderer='redshift',
      renderer_version=redshift_version,
      render_current_frame=False
    )

    if input_node.parm('trange').evalAsString() == 'off':
      current_frame = hou.frame()
      result['frame_begin'] = current_frame
      result['frame_end'] = current_frame
      result['step'] = 1
      # Resolution limits only apply to non- and limited-commercial, so "Render Current Frame"
      # isn't needed otherwise.
      result['render_current_frame'] = (hou.licenseCategory() != hou.licenseCategoryType.Commercial)
    else:
      result['frame_begin'] = input_node.parm('f1').eval()
      result['frame_end'] = input_node.parm('f2').eval()
      result['step'] = input_node.parm('f3').eval()

    return result

  @staticmethod
  def fetch_data_from_simulation_node(input_node):
    """Collects data from simulation input node.

    Assumes that input node is a simulation node

    Args:
      input_node: hou.Node, a simulation node.

    Returns:
      {str, object}, Submission parameters.
    """
    output_parm_names = dict(
      output='dopoutput',
      filecache='file')
    parm_name = output_parm_names.get(input_node.type().name())
    output_dir = input_node.parm(parm_name).unexpandedString()

    result = dict(
      output_filename=os.path.basename(output_dir),
      renderer='simulation',
      renderer_version=hou.applicationVersion(),
      render_current_frame=False
    )

    result['frame_begin'] = input_node.parm('f1').eval()
    result['frame_end'] = input_node.parm('f2').eval()
    result['step'] = input_node.parm('f3').eval()

    return result

  def fetch_data_from_source(self):
    """Identifies the input node type and collects data.

    Returns:
      {str, object}, Submission parameters.
    """
    input_node = get_render_node(self.node)
    node_type = get_type_of_input_node(input_node)
    if node_type == 'Mantra':
      return self.fetch_data_from_mantra_node(input_node)
    elif node_type == 'Arnold':
      return self.fetch_data_from_arnold_node(input_node)
    elif node_type == 'Redshift':
      return self.fetch_data_from_redshift_node(input_node)
    elif node_type == 'Simulation':
      return self.fetch_data_from_simulation_node(input_node)
    else:
      raise ParameterError('Input node has to be Mantra, Arnold or Redshift node.')

  def get_raw_params(self):
    """Collects submission parameters.

    Returns:
      {str, object}, Submission parameters.
    """
    source_data = self.fetch_data_from_source()

    if self.node.parm('override_frange').evalAsInt():
      frame_begin = int(self.node.parm('frame_rangex').evalAsInt())
      frame_end = int(self.node.parm('frame_rangey').evalAsInt())
      step = int(self.node.parm('frame_rangez').evalAsInt())
    else:
      frame_begin = source_data['frame_begin']
      frame_end = source_data['frame_end']
      step = source_data['step']

    project_name = get_project_name(self.node)

    if self.node.parm('override_filename').eval():
      output_filename = self.node.parm('output_filename').unexpandedString()
    else:
      output_filename = source_data['output_filename']

    machine_type = self.node.parm('machine_type').evalAsString()
    if not machine_type:
      raise ParameterError('Please select machine type again.')

    houdini_version = hou.applicationVersion()

    dependencies = self.get_dependencies(frame_begin, frame_end, step)
    if self.node.parm('auxiliary_files').evalAsInt():
      extra_assets = file_select_dialog.FileSelectDialog.get_extra_assets(project_name)
      if not extra_assets:
        raise ParameterError('Please select auxiliary files.')
      dependencies |= set(extra_assets)

    scene_info=dict(
      dependencies=list(dependencies),
      houdini_version='Houdini%d.%d' % (houdini_version[0], houdini_version[1]),
      houdini_build_version='%d.%d.%d' % houdini_version,
      renderer_version=source_data['renderer_version'],
      render_current_frame=source_data['render_current_frame']
    )

    if hou.getenv('HOUDINI_OTLSCAN_PATH'):
      scene_info['otlscan_path'] = hou.getenv('HOUDINI_OTLSCAN_PATH')
    if hou.getenv('HOUDINI_OTL_PATH'):
      scene_info['otl_path'] = hou.getenv('HOUDINI_OTL_PATH')

    params_to_send = dict(
      plugin_version=__version__,
      upload_only=self.node.parm('upload_only').evalAsInt(),
      skip_download=self.node.parm('skip_download').evalAsInt(),
      chunk_size=self.node.parm('chunk_size').evalAsInt(),
      instance_type=machine_type,
      num_instances=self.node.parm('num_instances').evalAsInt(),
      frame_begin=frame_begin,
      frame_end=frame_end,
      step=step,
      override_res=self.node.parm('override_resolution').evalAsInt(),
      xres=self.node.parm('resolutionx').evalAsInt(),
      yres=self.node.parm('resolutiony').evalAsInt(),
      notify_complete=self.node.parm('notify_completion').evalAsInt(),
      use_standalone=self.node.parm('use_standalone').evalAsInt(),
      priority=self.node.parm('job_priority').evalAsInt(),
      render_node=get_render_node(self.node).path(),
      output_dir=self.node.parm('output_dir').evalAsString(),
      output_filename=output_filename,
      proj_name=project_name,
      renderer=source_data['renderer'],
      scene_info=scene_info
    )

    if self.node.parm('has_parent').evalAsInt():
      params_to_send['parent_id'] = self.node.parm('parent_id').evalAsInt()

    return params_to_send


def post_submit_job(node):
  """Function called after successful submission of job.

  Args:
    node: hou.Node, Zync node to be updated.
  """
  ZyncConnection().get_project_list(force_fetch=True)
  node.parm('create_project').set(False)


def update_estimated_cost(node):
  """Updates estimated cost of hour of computation using given amount of
     machines and given machine type.

  Args:
    node: hou.Node, Node to be updated.
  """
  renderer = get_type_of_input_node(get_render_node(node)).lower()
  num_instances = node.parm('num_instances').evalAsInt()
  machine_type = node.parm('machine_type').evalAsString()
  unit_cost = ZyncConnection().get_unit_price_of_machine(renderer, machine_type)
  if unit_cost:
    price_per_hour = unit_cost * num_instances
    text = r'Est. cost per hour: \$%.2f' % price_per_hour
  else:
    text = 'Est. cost per hour is not available.'
  node.parm('est_cost').set(text)


def populate_machine_type(node):
  """Populates list of machine types available for user.

  In Houdini menus are represented as lists. Each entry in menu is represented
  by two elements in the list. First is a value of the entry. The other is user
  visible value.

  Args:
    node: hou.Node, Zync node to be updated.

  Returns:
    [str], List representing menu entries.
  """
  update_estimated_cost(node)
  renderer = get_type_of_input_node(get_render_node(node)).lower()

  data = ZyncConnection().get_machine_types(renderer=renderer)
  instances = [
      (v['order'],"%s ($%s)" %(k, v['cost']), k)
      for k, v in data.iteritems()]
  instances = sorted(instances)
  return [k for i in instances for k in (i[2], i[1])]


def populate_project(_):
  """Populates list of projects available for user.

  In Houdini menus are represented as lists. Each entry in menu is represented
  by two elements in the list. First is a value of the entry. The other is user
  visible value.

  Args:
    _: Ignored argument

  Returns:
    [str], List representing menu entries.
  """
  project_list = ZyncConnection().get_project_list()
  return [k for i in project_list for k in (i['name'], i['name']) if i['name']]


# Fill menu callback registry.
menu_callbacks = dict(
    machine_type=populate_machine_type,
    project=populate_project
)


def populate_menu(node, parm, **_):
  """Fill menu callback hub. Determines proper callback on parm name.

  Args:
    node: hou.Node, Node containing parameter.
    parm: hou.Parm, Parameter to be populated.
    **_: Ignored kwargs.

  Returns:
    [str], List representing menu entries.
  """
  try:
    return menu_callbacks[parm.name()](node)
  except Exception as e:
    hou.ui.displayMessage(title='Connection Error', text=str(e),
                          severity=hou.severityType.Error)
    return []


def update_node_login(node):
  """Updates a single node with login data.

  Args:
    node: hou.Node, Node to be updated.
  """
  if ZyncConnection().is_logged_in():
    # Hide login button, make logout visible
    node.parm('logged_name1').set(ZyncConnection().get_user_email())
    node.parm('logged_name2').set(ZyncConnection().get_site_name())
    node.parm('logged_in').set(1)
  else:
    node.parm('logged_in').set(0)


def update_all_node_login(node_type):
  """Updates login data in all instances of the type.

  Args:
    node_type: hou.NodeType, Type of the nodes to be updated
  """
  zync_node_types = dict(
    zync_render='Driver',
    zync_sim='Dop',
    zync_sim_sop='Sop'
  )

  for zync_node_name in zync_node_types.keys():
    category_name = zync_node_types[zync_node_name]
    category = hou.nodeTypeCategories()[category_name]
    node_type = hou.nodeType(category, zync_node_name)
    if node_type:
      for node in node_type.instances():
        update_node_login(node)


def get_render_node(node):
  """Gets rendering node attached to Zync node.

  Zync node has two ways to set rendering node. One is to connect node as
  input. Other is to set `source` parameter of Zync node.

  Args:
    node: hou.Node, Zync node

  Returns:
    hou.Node, ROP node used to render Zync job.
  """
  input_node_path = node.parm('source').evalAsString()
  return hou.node(input_node_path)


def get_type_of_input_node(input_node):
  """Gets rendering node attached to Zync node.

  Zync node has two ways to set rendering node. One is to connect node as
  input. Other is to set `source` parameter of Zync node.

  Args:
    input_node: hou.Node, Zync node

  Returns:
    hou.Node, ROP node used to render Zync job.
  """
  if input_node:
    input_node_type = input_node.type().name()
    if input_node_type == 'ifd':
      return 'Mantra'
    elif input_node_type == 'arnold':
      return 'Arnold'
    elif input_node_type == 'Redshift_ROP':
      return 'Redshift'
    elif input_node_type in ['output', 'filecache']:
      return 'Simulation'

  return 'Unknown'


def update_input_node(node):
  node.parm('render_type').set(get_type_of_input_node(get_render_node(node)))


def get_project_name(node):
  if node.parm('create_project').evalAsInt():
    return node.parm('new_project_name').evalAsString()
  else:
    project_name = node.parm('project').evalAsString()
    if not project_name:
      raise ParameterError('Please select project again.')
    return project_name


# Parms callbacks


def login_callback(node, **_):
  """Tries to log user in. Updates all nodes to login state.

  Args:
    node: hou.Node, Sender of the callback.
    **_: Ignored args.
  """
  # TODO(cipriano) Re-enable this once version check bug is resolved.
  #if not zync.is_latest_version([('zync_houdini', __version__)]):
  #  message = ("Your plugin is not up to date. Please update your plugin "
  #             "and restart Houdini to log in.")
  #  hou.ui.displayMessage(message)
  #  return
  ZyncConnection().login()
  update_all_node_login(node.type())
  _disable_standalone(node, ZyncConnection().is_v2())


def _disable_standalone(node, disable):
  use_standalone = node.parm('use_standalone')
  if disable:
    if use_standalone.isLocked:
      use_standalone.lock(False)
    use_standalone.set(False)
  use_standalone.lock(disable)


def logout_callback(node, **_):
  """Tries to logout user in. Updates all nodes to login state.

  Args:
    node: hou.Node, Sender of the callback.
    **_: Ignored args.
  """
  ZyncConnection().logout()
  update_all_node_login(node.type())


def cost_calculator_callback(**_):
  """Opens browser with price calculator.

  Args:
    **_: Ignored params.
  """
  webbrowser.open('http://zync.cloudpricingcalculator.appspot.com/')


def open_site_callback(**_):
  """Opens browser with the Zync Web Console.

  Args:
    **_: Ignored params.
  """
  webbrowser.open(zync.ZYNC_URL)


def zync_render_callback(node, **_):
  """Submits job to Zync.

  Args:
    node: hou.Node, Node used to submit job.
    **_: Ignored kwargs.
  """
  try:
    if hou.hipFile.hasUnsavedChanges():
      save_file_response = hou.ui.displayMessage(
          "There are some unsaved changes. Do you want to save the file before "
          "submitting to Zync?", buttons=("Yes", "No", "Cancel"), close_choice=2)
      if save_file_response == 2:
        return
      if save_file_response == 0:
        hou.hipFile.save()

    ZyncConnection().submit_job(node)
  except Exception as e:
    hou.ui.displayMessage(text=str(e), title='Job submission failed')


def select_auxiliary_files_callback(node, **_):
  """Shows dialog to select extra files before submission.

  Args:
    node: hou.Node, Node used to submit job.
    **_: Ignored kwargs.
  """
  project_name = get_project_name(node)
  node.dialog = file_select_dialog.FileSelectDialog(project_name, hou.ui.mainQtWindow())
  node.dialog.exec_()


def update_projects_list_callback(**_):
  """Fetches new list of existing projects.

  Args:
    **_: Ignored kwargs.
  """
  ZyncConnection().get_project_list(force_fetch=True)


def num_instances_callback(node, **_):
  """Updates estimated cost of rendering.

  Args:
    node: hou.Node, Node calling the update.
    **_: Other parameters
  """
  update_estimated_cost(node)


def source_callback(node, **_):
  update_input_node(node)


def open_help_callback(node, parm_name, **_):
  """Opens browser with context help for a parm

  Args:
    node: hou.Node, A parent node..
    parm_name: str, Name of the parm
    **_: Other parameters
  """
  base_url = 'https://docs.zyncrender.com/'
  context_help = dict(
    standalone_help='faq#q-what-is-the-render-workflow-when-submitting-a-render-job-from-houdini',
    upload_only_help='faq#q-what-does-upload-only-job-mean',
    skip_download_help='faq#q-can-i-skip-downloading-large-outputs',
  )
  webbrowser.open(base_url + context_help.get(parm_name, ''))


# Parameter callbacks registry.
callbacks = dict(
    login=login_callback,
    logout=logout_callback,
    open_site=open_site_callback,
    cost_calculator=cost_calculator_callback,
    zync_render=zync_render_callback,
    num_instances=num_instances_callback,
    source=source_callback,
    select_auxiliary_files=select_auxiliary_files_callback,
    update_projects_list=update_projects_list_callback,
    standalone_help=open_help_callback,
    upload_only_help=open_help_callback,
    skip_download_help=open_help_callback
)


def action_callback(**kwargs):
  """Callback hub for parameters. Callback is taken form `callback` registry
     using name of the parm.

  Args:
    **kwargs: Keyword parameters passed to the proper callback. Have to contain
              'parm_name' entry.
  """
  try:
    callbacks[kwargs['parm_name']](**kwargs)
  except Exception as e:
    hou.ui.displayMessage(title='Connection Error', text=str(e),
                          severity=hou.severityType.Error)


# Houdini node callbacks


def on_input_changed_callback(node, **_):
  """Callback on connection/disconnection source node of Zync node

  Args:
    node: hou.Node, Zync node.
    **_: Other parameters
  """
  inputs = node.inputs()
  input_node = None

  if inputs:
    input_node = inputs[0]

  node.parm('source').set(input_node.path() if input_node else '')

  update_input_node(node)


def on_created_callback(node, **_):
  """Called when Zync node is created.

  Args:
    node: hou.Node, Zync node.
    **_: Other parameters
  """
  update_node_login(node)


def on_loaded_callback(node, **_):
  """Called when Zync node is loaded.

  Args:
    node: hou.Node, Zync node.
    **_: Other parameters
  """
  update_node_login(node)

"""
"""


import copy
import os
import numpy as np
import xarray as xr

from ..calc import CalcManager
from ..utils.dataset_util import check_dims, get_dim_axis 
from ..utils.dataset_util import merge_coordinates
from ..utils.datetime_util import decode_months_since
from ..utils.io_util import extract_cmip6_variables, get_filename_from_drs
from ..utils.io_util import load_cmip6_output

from . import _DIM_COORDINATES, _VARS_NAME



class Experiment:
    def __init__(self, path=None, fmesh=None):
        self.path = path 
        self.fmesh = fmesh

        # core dataset properties
        self._dims = dict()
        self._coords = dict()
        self._chunks = None                       # dask chunks to split large datasets

        self._arrays = dict()                     # dict-like (could be xr.Dataset) obj
        self._mesh = xr.Dataset()                 # dict-like (should be xr.Dataset) obj
        self._calc = CalcManager(dataset=self)    # instance to compute off-line diag.

        # loading options
        self._unused_dims = list()


    def __getitem__(self, var):
        if var in self.variables:
            if var in self.arrays:
                variable = self.arrays[var]
            elif var in self._mesh.variables:
                variable = self._mesh[var]
            elif var in self.coords:
                variable = self.coords[var]
            else:
                lres = self.load_variable(var, chunks=self._chunks)
                if lres is not None:
                    variable = self.arrays[var]
                else:
                    raise KeyError("'{}' not found in the experiment.".format(var))
            
            for dim in self._unused_dims:
                if dim in variable.dims:
                    variable = variable.isel({dim:0})
            
            self.add_variable(var, variable)
            array = self.arrays[var]

        else:
            if not self._calc.is_calculable(var):
                raise KeyError("'{}' is not a variable of the experiment.".format(var) +
                    "Available variables: {}".format(self.variables))
            else:
                array = self.calculate(var)

        # linear interpolation if needed      
        for d in array.dims :
            c = _DIM_COORDINATES.get(d, d)
            if c in self.coords:
                if not np.alltrue(array[d].data == self.coords[c][d].data):
                    array = array.interp(**{d: self.coords[c]}, 
                                         kwargs={"fill_value": "extrapolate"})

        return array


    def __setitem__(self, var, values):
        # TODO: add test on dimensions
        if isinstance(values, xr.DataArray):
            self.add_variable(var, values)
        else:
            raise TypeError("Values should be a DataArray not {}".format(type(values)))


    # abstract method(s)
    def load(self, chunks={}, replace_dict={}):
        raise Exception("'load' function not implemented.")


    @property
    def dims(self):
        return self._dims

    @property
    def coords(self):
        return self._coords

    @property
    def attrs(self):
        return {}

    @property
    def arrays(self):
        return self._arrays

    @arrays.setter
    def arrays(self, value):
        self._arrays = value

    @property
    def variables(self):
        return list(self._arrays) + list(self._mesh) + list(self.coords)

    def calculate(self, var):
        return self._calc.calculate(var)

    def where(self, conds, other=np.nan, drop=False):
        dataset = xr.Dataset(coords=self.coords)
        
        for v in self.variables:
            if v not in dataset.dims:  
                # check dimensions shape and size
                if set(conds.dims) <= set(self[v].dims):
                    indx, skpd = get_dim_axis(self, self[v].dims, skip_notfound=True)
                    
                    var_shpe = np.delete(self[v].shape, skpd)
                    shpe     = np.take(list(self.dims.values()), indx)
                    
                    if np.alltrue(shpe == var_shpe): 
                        arr = self[v].where(conds, other, drop)
                        dataset[v] = (arr.dims, arr.data)
                        dataset[v].attrs = arr.attrs

                elif check_dims(self[v], dataset.dims):
                    arr = self[v]
                    dataset[v] = (arr.dims, arr.data)
                    dataset[v].attrs = arr.attrs
        
        return dataset

    def rename(self, name_dict=None, **names):
        if isinstance(name_dict, dict):
            names = name_dict
        
        for name in names:
            if name in self.arrays:
                if isinstance(self.arrays, dict):
                    for v in self.arrays:
                        self.arrays[v] = self.arrays[v].rename(**{name:names[name]})
                else:
                    self.arrays = self.arrays.rename_dims(**{name:names[name]})
            if name in self._coords:
                self._coords[names[name]] = self._coords[name]
                del self._coords[name]

    def rename_dims(self, name_dict=None, **names):
        if isinstance(name_dict, dict):
            names = name_dict
        
        for name in names:
            if isinstance(self.arrays, dict):
                for v in self.arrays:
                    self.arrays[v] = self.arrays[v].rename(**{name:names[name]})
            else:
                self.arrays = self.arrays.rename_dims(**{name:names[name]})

        for co in self._coords:
            for name in names:
                if name in self._coords[co].dims:
                    newc = self._coords[co].rename(**{name:names[name]})
                    newc.coords[name] = newc.coords[names[name]]
                    del newc.coords[names[name]]

                    self._coords[name] = newc

    def add_variable(self, var, arr, rename_dims=True):
        """
        Add new variable in the private self.arrays dictionary.
        If the variable already exists, the function simply return 
        the desired array. 
        Warning: special treatment are made to get the real variable 
        name..
        """
        if var in list(_VARS_NAME[type(self).__name__].keys()):
            newvar = _VARS_NAME[type(self).__name__][var]
        else:
            newvar = var

        if rename_dims:
            rename_dict = dict()
            for vn in _VARS_NAME[type(self).__name__]:
                if vn in list(arr.dims) + list(arr.coords):
                    rename_dict[vn] = _VARS_NAME[type(self).__name__][vn]
            arr = arr.rename(rename_dict)

            if arr.name in _VARS_NAME[type(self).__name__]:
                arr.name = _VARS_NAME[type(self).__name__][arr.name]

        if newvar not in list(self.arrays):
            self.arrays[newvar] = arr
        
        return newvar, arr

    def add_coordinate(self, var, arr, rename_dims=True):
        """
        Add new coordinate in the private self._coords dictionary.
        If the variable already exists, the function simply return 
        the desired array. 
        Warning: special treatment are made to get the real variable 
        name..
        """
        if var in list(_VARS_NAME[type(self).__name__].keys()):
            newvar = _VARS_NAME[type(self).__name__][var]
        else:
            newvar = var

        if rename_dims:
            rename_dict = dict()
            for vn in _VARS_NAME[type(self).__name__]:
                if vn in list(arr.dims) + list(arr.coords):
                    rename_dict[vn] = _VARS_NAME[type(self).__name__][vn]
            arr = arr.rename(rename_dict)

            if arr.name in _VARS_NAME[type(self).__name__]:
                arr.name = _VARS_NAME[type(self).__name__][arr.name]

        if newvar not in self.arrays:
            self._coords[newvar] = arr
        
        return newvar, arr

    def load_variable(self, var, chunks=None):
        """
        For Experiment instance using lazy loading and which all DataArrays are
        not directly loaded.
        """
        pass


class SingleDatasetExperiment(Experiment):
    def __init__(self, path=None, fmesh=None):
        super().__init__(path, fmesh)

    @property
    def arrays(self):
        return self._arrays

    @arrays.setter
    def arrays(self, ds):
        self._arrays = ds
        self._coords = ds.coords
        self._dims   = ds.dims

    # abstract method(s) definition
    def load(self, chunks={}, replace_dict={}):
        """Loading output files."""
        try :
            ds = xr.open_mfdataset(self.path)
        except ValueError:
            ds = xr.open_mfdataset(self.path, decode_times=False)
            ds = ds.assign_coords( {'time': decode_months_since(ds['time'])} )
        
        ds = ds.chunk(chunks)
        
        if self.fmesh:
            mesh = xr.open_dataset(self.fmesh)
            code_info = merge_coordinates(mesh, ds.coords)

        # replace some variables values
        for var in replace_dict:
            newvar = replace_dict[var]   
            inside = (var in mesh or var in mesh.dims)
            inside = inside & (newvar in mesh or newvar in mesh.dims)
            
            if inside:
                mesh = mesh.assign({var: mesh[newvar]})

            inside = (var in ds or var in ds.dims)
            inside = inside & (newvar in ds or newvar in ds.dims)
            
            if inside:
                ds = ds.assign({var: mesh[newvar]})

        # rename some vars, coords or dims
        rename_dict = dict()
        for v, n in _VARS_NAME[type(self).__name__].items():
            if v in ds:
                rename_dict[v] = n
        ds = ds.rename(rename_dict)

        # add into object placeholders
        self._arrays = ds
        self._coords = ds.coords
        self._dims   = ds.dims

        if code_info == -1:
            print("Warning: mesh and dataset coordinates are not everywhere equal.")
        else:
            self._mesh = mesh


class CMIPExperiment(Experiment):
    """
    Experiment data container based on CMIP6 protocole.
    """
    def __init__(self, path=None, fmesh=None):
        super().__init__(path, fmesh)

        # child properties
        self._drs  = dict()         # data reference syntax: variableID_tableID_ .. .nc


    # abstract method(s) definition
    def load(self, chunks={}, replace_dict={}):
        """Loading output files."""

        self._chunks = chunks
        self._drs = load_cmip6_output(self.path)
        
        if self.fmesh:
            mesh = xr.open_dataset(self.fmesh)
            
            # replace some variables values
            for var in replace_dict:
                newvar = replace_dict[var]
                inside = (var in mesh or var in mesh.dims)
                inside = inside & (newvar in mesh or newvar in mesh.dims)
                if inside:
                    mesh = mesh.assign({var: mesh[newvar]})
            
            self._mesh = mesh


    @property
    def variables(self):
        return super().variables + self._drs.get('variable_id', [])

    def load_variable(self, var, chunks={}):
        if not self._drs :
            self._drs = load_cmip6_output(self.path)

        if var not in self._drs['variable_id']:
            raise Exception("No file match `variable_id = {}`".format(var) + 
                            "in directory: {}".format(self.path))
        
        fname = get_filename_from_drs(var, self._drs)
        abspath = os.path.join(self.path, fname)
        ds = xr.open_dataset(abspath, chunks=chunks)
        
        # update experiment dims and coords
        for d in ds.dims:
            if d not in self.dims:
                self._dims[d] = ds.dims[d]
        
        for c in ds.coords:
            if c not in self.coords:
                _ = self.add_coordinate(c, ds.coords[c], rename_dims=True)
        
        # finally link DataArray in a container
        self.add_variable(var, ds[var])
        
        return ds[var]

    def extract_vars(self, variables):
        """
        return only a limited variables list stored in its _drs property.
        """   
        ndrs = extract_cmip6_variables(variables, 'variable_id', self._drs)
        experiment = copy.deepcopy(self)
        experiment._drs = ndrs

        return experiment


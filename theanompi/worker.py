from __future__ import absolute_import

from theanompi.lib.base import MPI_GPU_Process

class BSP_Worker(MPI_GPU_Process):
    
    def __init__(self, device, sync_type, exch_strategy):
        MPI_GPU_Process.__init__(self, device) # setup ctx, comm
        
        self.get_intranode_comm() # setup gpucomm
        
        self.sync_type = sync_type
        
        self.exch_strategy = exch_strategy 
        
        self.verbose = (self.rank==0)
        
    def build(self, model, config):
        
        from theanompi.lib.helper_funcs import check_model, check_model_cdd
        
        # check model has necessary attributes
        check_model(model)
        # construct model train function based on sync rule
        model.compile_iter_fns(self.sync_type)
        
        if self.sync_type=='avg': 
            model.scale_lr(self.size)
        
        from theanompi.lib.recorder import Recorder
        self.recorder = Recorder(self.comm, printFreq=40, modelname=config['mname'], verbose=self.verbose)
        
        # choose the type of exchanger
        from theanompi.lib.exchanger import BSP_Exchanger
        self.exchanger = BSP_Exchanger(self.comm, self.gpucomm, self.exch_strategy, self.sync_type, self.ctx, model)
            
            
    def BSP_run(self, model):
        
        self.comm.Barrier()
        
        exchange_freq = 1 # iterations
        snapshot_freq = 5 # epochs
        snapshot_path = './snapshots/'
        recorder=self.recorder
        exchanger=self.exchanger
        
        from theanompi.lib.helper_funcs import save_model

        for epoch in range(model.n_epochs):
            
            model.epoch=epoch
            
            recorder.start_epoch()
            
            # train
            exch_iteration=0
            batch_i=0
            while batch_i <model.data.n_batch_train:
                
                for subb_i in range(model.n_subb):
        
                    out = model.train_iter(batch_i, recorder)
                    
                    if out!=None: 
                        batch_i = out
                    else:
                        batch_i+=1
                    
                    if exch_iteration % exchange_freq == 0: 
                        exchanger.exchange(recorder)
                    exch_iteration+=1
                    
                recorder.print_train_info(batch_i*self.size)
            
            recorder.clear_train_info()
            
            model.reset_iter('train')
            
        
            # val
            
            self.comm.Barrier()
    
            for batch_j in range(model.data.n_batch_val):
                
                for subb_i in range(model.n_subb):
                    
                    model.val_iter(batch_i*self.size, recorder)

                
            model.reset_iter('val')
            
            recorder.gather_val_info()
        
            recorder.print_val_info(batch_i*self.size)
            model.current_info = recorder.get_latest_val_info()
            
            if self.rank==0: recorder.save(batch_i*self.size, model.shared_lr.get_value())
            
            if epoch % snapshot_freq == 0 and self.rank==0: save_model(model, snapshot_path, verbose=self.verbose)
            
            model.adjust_hyperp(epoch)
            
            if hasattr(model,'print_info'):
                model.print_info(recorder)
            
            recorder.end_epoch(batch_i*self.size, epoch)
            
        model.cleanup()

        
if __name__ == '__main__':
    
    import sys
    device = sys.argv[1]
    sync_type = sys.argv[2]
    exch_strategy = sys.argv[3]
    modelfile = sys.argv[4]
    modelclass = sys.argv[5]
    try:
        cpulist = sys.argv[6]
    except:
        pass
    else: # optional binding cores using hwloc
        from theanompi.lib.hwloc_utils import bind_to_socket_mem,detect_socket_num
        bind_to_socket_mem(cpulist, label='train')
        detect_socket_num(debug=True, label='train')
    
    worker = BSP_Worker(device, sync_type, exch_strategy)
    
    config={}
    config['verbose'] = (worker.rank==0)
    config['rank'] = worker.rank
    config['size'] = worker.size
    config['mname'] = modelclass
    
    import importlib
    mod = importlib.import_module(modelfile)
    modcls = getattr(mod, modelclass)

    model = modcls(config)

    worker.build(model, config)
    
    worker.BSP_run(model)